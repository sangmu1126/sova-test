"""Object-storage handoff world between two clouds: AWS S3 and Cloud Storage,
in either direction.

Each side is handled the way its provider works: an S3 bucket policy and IAM
roles on AWS, bucket IAM bindings and service accounts on GCS. The content,
state and route observations are the S3 world's, through each side's client.
"""

import json
from uuid import uuid4

from botocore.exceptions import ClientError
from gcs_world import GcsWorld, _settling, until
from s3_world import LARGE, Route, S3World, _Bytes, application_access

from sova.adapters.gcs_store import STORAGE, GcsBucketIamAccess, GcsS3Client
from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.s3_offline_copy import S3OfflineCopyAdapter


class CloudPairWorld(S3World):
    def __init__(self, aws, gcs, *, source, propagation_seconds=60,
                 adapter_id="cross-cloud-objects"):
        if source not in ("aws", "gcs"):
            raise ValueError("source is 'aws' or 'gcs'")
        self.kind = {"source": source, "target": "gcs" if source == "aws" else "aws"}
        self.aws, self.g, self.adapter_id = aws, gcs, adapter_id
        self.propagation_seconds, self.accept_history_loss = propagation_seconds, False
        suffix = uuid4().hex[:6]
        self.buckets = {side: f"sova-test-{side[:3]}-{gcs['run']}-{suffix}"
                        for side in ("source", "target")}
        self.root = {side: (aws["root"] if kind == "aws"
                            else GcsS3Client(gcs["admin"], region=gcs["region"]))
                     for side, kind in self.kind.items()}
        self.principals = {side: (aws["principals"] if kind == "aws" else gcs["members"])
                           for side, kind in self.kind.items()}
        self.base_bindings = None
        self._create("source", grant_writers=True)
        if self.kind["target"] == "gcs":
            self._create("target", grant_writers=False)  # the adapter cannot create GCS buckets
        app, bucket = self._as("source", "app"), self.buckets["source"]
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

    # -- per provider -----------------------------------------------------------------
    def _create(self, side, *, grant_writers):
        bucket = self.buckets[side]
        if self.kind[side] == "aws":
            self.aws["create_bucket"](self.root[side], bucket)
            if grant_writers:
                self.base_policy = application_access(
                    bucket, sorted(self.principals[side].values()))
                self.root[side].put_bucket_policy(Bucket=bucket,
                                                  Policy=json.dumps(self.base_policy))
            return
        admin = self.g["admin"]
        admin.post(f"{STORAGE}/b", {
            "name": bucket, "location": self.g["region"], "labels": {"sova-test": self.g["run"]},
            "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True},
                                 "publicAccessPrevention": "enforced"}},
            params={"project": self.g["project"]})
        if grant_writers:
            policy = admin.get(f"{STORAGE}/b/{bucket}/iam",
                               params={"optionsRequestedPolicyVersion": 3})
            policy["bindings"].append({"role": "roles/storage.objectAdmin",
                                       "members": sorted(self.g["members"].values())})
            admin.call("PUT", f"{STORAGE}/b/{bucket}/iam", json={**policy, "version": 3})
            self.base_bindings = admin.get(f"{STORAGE}/b/{bucket}/iam")["bindings"]

    def _as(self, side, writer):
        if self.kind[side] == "aws":
            return self.aws["as"](writer)
        return GcsS3Client(self.g["as"][writer], region=self.g["region"])

    def _access(self, side):
        if self.kind[side] == "aws":
            return S3BucketPolicyAccess(self.root[side], self.buckets[side],
                                        principals=self.principals[side])
        return GcsBucketIamAccess(self.g["admin"], self.buckets[side],
                                  principals=self.principals[side],
                                  propagation_seconds=self.propagation_seconds)

    def side(self, name):
        return self.root[name], self.buckets[name], self._access(name)

    def _adapter(self, source, target):
        return S3OfflineCopyAdapter(
            self.adapter_id, source=self.side(source), target=self.side(target),
            app_principal="app", writer_principals=("app", "other"), route=self._route)

    def _gcs_view(self, side):
        """The GCS world's own operations, pointed at this side's bucket."""
        view = GcsWorld.__new__(GcsWorld)
        view.g, view.root = self.g, {"source": self.root[side]}
        view.buckets = {"source": self.buckets[side]}
        view.base_bindings, view.propagation_seconds = self.base_bindings, self.propagation_seconds
        view.pending = self.pending
        return view

    def can_write(self, side, writer):
        if self.kind[side] == "gcs":
            return self._gcs_view(side).can_write("source", writer)
        if not self._exists(side):
            return False
        try:
            self._as(side, writer).put_object(
                Bucket=self.buckets[side], Key="sova-write-probe", Body=b"",
                IfMatch='"00000000000000000000000000000000"')
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in ("NoSuchKey", "PreconditionFailed"):
                return True
            if code == "AccessDenied":
                return False
            raise
        raise AssertionError("a conditional probe wrote an object")

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
        keys = [o["Key"] for page in pages for o in page.get("Contents", ())][:count]
        for key in keys:
            app.delete_object(Bucket=bucket, Key=key)

    def _policy(self, side):
        if self.kind[side] == "gcs":
            return self._access(side).policy_text()
        return super()._policy(side)

    def _uploads(self, side):
        if not self._exists(side):
            return None
        pages = self.root[side].get_paginator("list_multipart_uploads").paginate(
            Bucket=self.buckets[side])
        return sorted(u["Key"] for page in pages for u in page.get("Uploads", ()))

    def reopen_source(self):
        if self.kind["source"] == "gcs":
            self._gcs_view("source").reopen_source()
            return
        self.root["source"].put_bucket_policy(Bucket=self.buckets["source"],
                                              Policy=json.dumps(self.base_policy))
        until(lambda: self.can_write("source", "app"), "the restored policy took effect")

    def open_pending_write(self):
        if self.kind["source"] == "gcs":
            view = self._gcs_view("source")
            view.open_pending_write()
            self.pending = view.pending
            return
        app = self._as("source", "app")
        upload = app.create_multipart_upload(Bucket=self.buckets["source"],
                                             Key="exports/pending.bin")
        app.upload_part(Bucket=self.buckets["source"], Key="exports/pending.bin",
                        UploadId=upload["UploadId"], PartNumber=1, Body=b"p" * 1024)
        self.pending = upload["UploadId"]

    def resolve_pending_write(self):
        if self.kind["source"] == "gcs":
            self._gcs_view("source").resolve_pending_write()
            return
        self.root["source"].abort_multipart_upload(Bucket=self.buckets["source"],
                                                   Key="exports/pending.bin",
                                                   UploadId=self.pending)

    def cleanup(self):
        for side, kind in self.kind.items():
            if kind == "gcs":
                self._gcs_view(side).cleanup()
                continue
            client, bucket = self.root[side], self.buckets[side]
            try:
                pages = client.get_paginator("list_object_versions").paginate(Bucket=bucket)
                for page in pages:
                    items = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                             for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
                    if items:
                        client.delete_objects(Bucket=bucket, Delete={"Objects": items})
                for upload in client.list_multipart_uploads(Bucket=bucket).get("Uploads", []):
                    client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"],
                                                  UploadId=upload["UploadId"])
                client.delete_bucket(Bucket=bucket)
            except ClientError as error:
                if error.response["Error"]["Code"] != "NoSuchBucket":
                    raise
