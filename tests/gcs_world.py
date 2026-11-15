"""Object-storage handoff world with a GCS bucket as the source and an
S3-compatible store (the local gateway) as the target.

Content, state and route observation are the S3 world's; what differs is the
source side: the bucket and its writers' grants are GCS IAM, the writers are
two service accounts the test acts as (impersonation, test-only), and a
pending write is an XML API multipart upload.
"""

import json
import time
from uuid import uuid4
import xml.etree.ElementTree as ElementTree

from botocore.exceptions import ClientError
from s3_world import LARGE, Route, S3World, _Bytes

from sova.adapters.gcs_store import STORAGE, UPLOAD, XML, GcsBucketIamAccess, GcsS3Client
from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.s3_offline_copy import S3OfflineCopyAdapter

SEED_WAIT_SECONDS = 180


class GcsWorld(S3World):
    def __init__(self, gcs, s3_stores, *, versioned=False, accept_history_loss=False,
                 adapter_id="gcs-offline-copy", propagation_seconds=420):
        self.g, self.s, self.adapter_id = gcs, s3_stores, adapter_id
        self.propagation_seconds = propagation_seconds
        suffix = uuid4().hex[:6]
        self.buckets = {"source": f"sova-test-src-{gcs['run']}-{suffix}",
                        "target": s3_stores["bucket_name"]("target", suffix)}
        self.root = {"source": GcsS3Client(gcs["admin"], region=gcs["region"]),
                     "target": s3_stores["root"]("target")}
        self.principals = {"source": gcs["members"], "target": s3_stores["principals"]["target"]}
        self.accept_history_loss = accept_history_loss
        admin, bucket = gcs["admin"], self.buckets["source"]
        admin.post(f"{STORAGE}/b", {
            "name": bucket, "location": gcs["region"], "labels": {"sova-test": gcs["run"]},
            "versioning": {"enabled": versioned},
            "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True},
                                 "publicAccessPrevention": "enforced"}},
            params={"project": gcs["project"]})
        policy = admin.get(f"{STORAGE}/b/{bucket}/iam", params={"optionsRequestedPolicyVersion": 3})
        policy["bindings"].append({"role": "roles/storage.objectAdmin",
                                   "members": sorted(gcs["members"].values())})
        policy["version"] = 3
        admin.call("PUT", f"{STORAGE}/b/{bucket}/iam", json=policy)
        self.base_bindings = admin.get(f"{STORAGE}/b/{bucket}/iam")["bindings"]
        s3_stores["create_bucket"](self.root["target"], self.buckets["target"])
        app = self._as("source", "app")
        for i in range(1, 11):
            _settling(lambda i=i: app.put_object(
                Bucket=bucket, Key=f"orders/{i:04d}.json",
                Body=json.dumps({"order": i, "amount": i * 10}).encode(),
                ContentType="application/json", Metadata={"customer": f"c-{i % 3}"}))
        _settling(lambda: app.upload_fileobj(_Bytes(b"x" * LARGE), bucket, "exports/large.bin",
                                             ExtraArgs={"ContentType": "application/octet-stream"}))
        self.next_order = 11
        self._route = Route(self.route_of("source"))
        self.pending = None

    def _as(self, side, writer):
        if side == "source":
            return GcsS3Client(self.g["as"][writer], region=self.g["region"])
        return self.s["as"](side, writer)

    def _access(self, side):
        if side == "source":
            return GcsBucketIamAccess(self.g["admin"], self.buckets["source"],
                                      principals=self.g["members"],
                                      propagation_seconds=self.propagation_seconds)
        return S3BucketPolicyAccess(self.root["target"], self.buckets["target"],
                                    principals=self.principals["target"])

    def side(self, name):
        return self.root[name], self.buckets[name], self._access(name)

    def _adapter(self, source, target):
        return S3OfflineCopyAdapter(
            self.adapter_id, source=self.side(source), target=self.side(target),
            app_principal="app", writer_principals=("app", "other"), route=self._route,
            accept_history_loss=self.accept_history_loss)

    def can_write(self, side, writer):
        """A write that can never succeed: ``ifGenerationMatch`` on a key that
        does not exist. An allowed writer gets a failed precondition, a denied
        one an access denial; nothing is ever written."""
        if side != "source":
            return super().can_write(side, writer)
        if not self._exists(side):
            return False
        rest = self.g["as"][writer]
        response = rest.session.request(
            "POST", f"{UPLOAD}/b/{self.buckets['source']}/o",
            params={"uploadType": "media", "name": "sova-write-probe", "ifGenerationMatch": "1"},
            data=b"", headers={"Content-Type": "application/octet-stream"}, timeout=30)
        if response.status_code == 412:
            return True
        if response.status_code in (401, 403):
            return False
        raise AssertionError(f"unexpected answer to the write probe: {response.status_code}")

    def business_write(self, count):
        app = self._as("source", "app")
        for i in range(self.next_order, self.next_order + count):
            app.put_object(Bucket=self.buckets["source"], Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i}).encode(),
                           ContentType="application/json")
        self.next_order += count

    def business_delete(self, count):
        app, bucket = self._as("source", "app"), self.buckets["source"]
        pages = app.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="orders/")
        keys = [o["Key"] for page in pages for o in page["Contents"]][:count]
        for key in keys:
            app.delete_object(Bucket=bucket, Key=key)

    def _policy(self, side):
        if side == "source":
            return self._access("source").policy_text()
        return super()._policy(side)

    def _uploads(self, side):
        if not self._exists(side):
            return None
        pages = self.root[side].get_paginator("list_multipart_uploads").paginate(
            Bucket=self.buckets[side])
        return sorted(u["Key"] for page in pages for u in page.get("Uploads", ()))

    def statements(self, side):
        return json.loads(self._policy(side))

    def reopen_source(self):
        """The source grants restored from before the handoff, markers gone."""
        admin, bucket = self.g["admin"], self.buckets["source"]
        policy = admin.get(f"{STORAGE}/b/{bucket}/iam", params={"optionsRequestedPolicyVersion": 3})
        admin.call("PUT", f"{STORAGE}/b/{bucket}/iam",
                   json={**policy, "version": 3, "bindings": self.base_bindings})
        labels = admin.get(f"{STORAGE}/b/{bucket}").get("labels") or {}
        self._access("source").lift([k[len("sova-handoff-"):] for k in labels
                                     if k.startswith("sova-handoff-")])
        # The reopening is real once the restored grant reaches the front ends.
        until(lambda: self.can_write("source", "app"), "the restored grant took effect")

    def open_pending_write(self):
        """An XML API multipart upload with one part, started by the application."""
        session = self.g["as"]["app"].session
        url = f"{XML}/{self.buckets['source']}/exports/pending.bin"
        started = session.request("POST", url, params={"uploads": ""}, timeout=30)
        assert started.status_code == 200, started.status_code
        upload = next(e.text for e in ElementTree.fromstring(started.content)
                      if e.tag.endswith("UploadId"))
        part = session.request("PUT", url, params={"partNumber": "1", "uploadId": upload},
                               data=b"p" * 1024, timeout=30)
        assert part.status_code == 200, part.status_code
        self.pending = upload

    def resolve_pending_write(self):
        response = self.g["admin"].session.request(
            "DELETE", f"{XML}/{self.buckets['source']}/exports/pending.bin",
            params={"uploadId": self.pending}, timeout=30)
        assert response.status_code in (200, 204), response.status_code

    def cleanup(self):
        """Delete the source bucket and everything in it (the target is local)."""
        admin, bucket = self.g["admin"], self.buckets["source"]
        try:
            client = self.root["source"]
            for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
                for version in page["Versions"]:
                    admin.delete(f"{STORAGE}/b/{bucket}/o/{_quote(version['Key'])}",
                                 params={"generation": version["VersionId"]}, missing_ok=True)
            for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
                for upload in page["Uploads"]:
                    admin.session.request("DELETE", f"{XML}/{bucket}/{upload['Key']}",
                                          params={"uploadId": upload["UploadId"]}, timeout=30)
        except ClientError:
            pass
        admin.delete(f"{STORAGE}/b/{bucket}", missing_ok=True)


def until(condition, what, seconds=SEED_WAIT_SECONDS):
    """Wait for an IAM change the world made to take effect (bounded)."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"not within {seconds} s: {what}"
        time.sleep(3)


def _settling(write):
    """Seed writes right after the bucket grant: a new grant reaches the
    storage front ends unevenly (some accept, some still refuse, measured)."""
    deadline = time.monotonic() + SEED_WAIT_SECONDS
    while True:
        try:
            return write()
        except ClientError:
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def _quote(key):
    from urllib.parse import quote

    return quote(key, safe="")
