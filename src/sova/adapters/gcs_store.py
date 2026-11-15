"""Google Cloud Storage as one side of the object-storage handoff.

``S3OfflineCopyAdapter`` holds the handoff logic (stage, fence, copy, compare,
activate) and talks to each side through an S3-shaped client and a
``BucketAccess``. This module gives a GCS bucket both, over the GCS JSON API,
so the same adapter moves a bucket between GCS and any S3-compatible store
in either direction:

``GcsS3Client``
    the subset of the S3 client the adapter uses, mapped onto GCS objects:
    bytes are read as stored (``Accept-Encoding: gzip``, no decompressive
    transcoding, which would otherwise hand back different bytes than the
    object holds) at the generation whose metadata was read; uploads are
    resumable and streamed; the content headers and user metadata map one to
    one; GCS has no object tags (an empty tag set is the truth); object
    retention and holds are reported like S3 object lock, so a source using
    them never verifies as copied; in-progress XML multipart uploads are
    listed through the XML API. A resumable upload session opened before the
    fence cannot complete after it: GCS checks the uploader's permission at
    completion (measured: 403), so such sessions need no listing.

``GcsBucketIamAccess``
    GCS has no deny statements in bucket policies. A handoff denies a writer
    by removing every bucket-level binding of that principal (etag-guarded,
    so a concurrent edit fails instead of being overwritten) and records its
    own marker as a bucket label; it opens the application by granting
    ``roles/storage.objectAdmin``. Whether a principal can write is never
    computed here: Policy Troubleshooter answers it from every policy that
    applies (project and folder grants, deny policies, conditions). Only
    ``CANNOT_ACCESS`` counts as denied and only ``CAN_ACCESS`` as writable;
    a conditional or unknown answer is neither. A writer that also holds a
    project-level grant therefore stays undenied, and the fence unconfirmed,
    until that grant is removed by its owner.

    Policy Troubleshooter answers from the stored policy, not from what the
    storage front ends enforce yet. IAM is eventually consistent: Google
    documents changes as typically taking effect within 2 minutes and in some
    cases 7 minutes or more, with a revoked role still usable meanwhile (and
    a just-granted one refused, which the tests measured). So every policy
    change the handoff makes records, in bucket labels, its time, a digest of
    the bindings it left and the bucket's metageneration after the label
    write itself (written with ``ifMetagenerationMatch``, so the number is
    exact); it waits ``propagation_seconds`` (default 420, the 7 minutes
    named). A denial or grant counts only once that much time has passed and
    both the bindings and the bucket are still as the handoff left them. A
    change by anyone else in between has no known time, so nothing is
    confirmed until the handoff writes again. (The policy etag cannot serve:
    on GCS it follows the bucket's metadata, so the label write changes it.) The wait is the real price of
    fencing by IAM on GCS; it falls on the first fencing step (pause), since
    later ones find nothing left to remove.
"""

import hashlib
import re
import time
from urllib.parse import quote
import xml.etree.ElementTree as ElementTree

from botocore.exceptions import ClientError

from sova.adapters.gcp_rest import GcpApiError, GcpRest
from sova.domain.contracts import ContractError, canonical_json

STORAGE = "https://storage.googleapis.com/storage/v1"
UPLOAD = "https://storage.googleapis.com/upload/storage/v1"
XML = "https://storage.googleapis.com"
TROUBLESHOOT = "https://policytroubleshooter.googleapis.com/v3/iam:troubleshoot"
_HEADERS = {"ContentType": "contentType", "CacheControl": "cacheControl",
            "ContentDisposition": "contentDisposition", "ContentEncoding": "contentEncoding",
            "ContentLanguage": "contentLanguage"}
_CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as resumable uploads require
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]")
_WRITE = ("storage.objects.create", "storage.objects.delete")
MARKER = "sova-handoff-"
CHANGED_AT, BINDINGS, METAGENERATION = MARKER + "at", MARKER + "bindings", MARKER + "meta"
_BOOKKEEPING = (CHANGED_AT, BINDINGS, METAGENERATION)
WRITER_ROLE = "roles/storage.objectAdmin"


def _error(status, code, operation):
    return ClientError({"Error": {"Code": code, "Message": ""},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class _Meta:
    def __init__(self, region):
        self.endpoint_url, self.region_name = XML, region


class _Body:
    """The stored bytes of one object generation, streamed undecoded."""

    def __init__(self, response):
        self.response = response

    def iter_chunks(self, size=_CHUNK):
        try:
            while True:
                chunk = self.response.raw.read(size, decode_content=False)
                if not chunk:
                    return
                yield chunk
        finally:
            self.response.close()

    def read(self, amount=None):
        if amount is None:
            return b"".join(self.iter_chunks())
        return self.response.raw.read(amount, decode_content=False)


class _Pages:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages(**kwargs)


class GcsS3Client:
    def __init__(self, rest, *, region=None):
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        self.rest, self.meta = rest, _Meta(region)

    # -- plumbing ----------------------------------------------------------------
    def _bucket_url(self, bucket):
        if not _BUCKET.fullmatch(bucket or ""):
            raise ContractError("GCS bucket name required")
        return f"{STORAGE}/b/{bucket}"

    def _object_url(self, bucket, key):
        return f"{self._bucket_url(bucket)}/o/{quote(key, safe='')}"

    def _call(self, operation, method, url, *, missing="NoSuchKey", **kwargs):
        try:
            return self.rest.call(method, url, **kwargs)
        except GcpApiError as error:
            if error.status == 404:
                raise _error(404, missing, operation) from None
            if error.status in (401, 403):
                raise _error(error.status, "AccessDenied", operation) from None
            if error.status == 412:
                raise _error(412, "PreconditionFailed", operation) from None
            raise

    def _bucket(self, bucket, operation):
        return self._call(operation, "GET", self._bucket_url(bucket), missing="NoSuchBucket")

    # -- buckets -----------------------------------------------------------------
    def head_bucket(self, Bucket):  # noqa: N803 - the S3 client's parameter names
        self._bucket(Bucket, "HeadBucket")
        return {}

    def get_bucket_versioning(self, Bucket):  # noqa: N803
        found = self._bucket(Bucket, "GetBucketVersioning")
        return {"Status": "Enabled"} if (found.get("versioning") or {}).get("enabled") else {}

    def put_bucket_versioning(self, Bucket, VersioningConfiguration):  # noqa: N803
        enabled = VersioningConfiguration.get("Status") == "Enabled"
        self._call("PutBucketVersioning", "PATCH", self._bucket_url(Bucket),
                   json={"versioning": {"enabled": enabled}}, missing="NoSuchBucket")

    def get_object_lock_configuration(self, Bucket):  # noqa: N803
        found = self._bucket(Bucket, "GetObjectLockConfiguration")
        retained = ((found.get("objectRetention") or {}).get("mode") == "Enabled"
                    or bool(found.get("retentionPolicy")))
        if not retained:
            raise _error(404, "ObjectLockConfigurationNotFoundError",
                         "GetObjectLockConfiguration")
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def create_bucket(self, **kwargs):
        raise ContractError("A GCS bucket is created by its owner, not by the handoff")

    def get_bucket_tagging(self, Bucket):  # noqa: N803
        labels = self._bucket(Bucket, "GetBucketTagging").get("labels") or {}
        if not labels:
            raise _error(404, "NoSuchTagSet", "GetBucketTagging")
        return {"TagSet": [{"Key": k, "Value": v} for k, v in sorted(labels.items())]}

    def put_bucket_tagging(self, Bucket, Tagging):  # noqa: N803
        """Bucket labels, the GCS counterpart of bucket tags (replaced as a set)."""
        current = self._bucket(Bucket, "PutBucketTagging").get("labels") or {}
        wanted = {t["Key"]: t["Value"] for t in Tagging.get("TagSet", ())}
        labels = {**dict.fromkeys(current), **wanted}  # None removes a label
        self._call("PutBucketTagging", "PATCH", self._bucket_url(Bucket),
                   json={"labels": labels}, missing="NoSuchBucket")

    # -- objects -----------------------------------------------------------------
    def _metadata(self, bucket, key, operation):
        return self._call(operation, "GET", self._object_url(bucket, key))

    def get_object(self, Bucket, Key):  # noqa: N803
        found = self._metadata(Bucket, Key, "GetObject")
        response = self.rest.session.request(
            "GET", self._object_url(Bucket, Key),
            params={"alt": "media", "generation": found["generation"]},
            headers={"Accept-Encoding": "gzip"}, stream=True, timeout=self.rest.timeout)
        if response.status_code != 200:
            response.close()
            code = {404: "NoSuchKey", 403: "AccessDenied"}.get(response.status_code,
                                                               str(response.status_code))
            raise _error(response.status_code, code, "GetObject")
        result = {"Body": _Body(response), "ContentLength": int(found.get("size", 0)),
                  "Metadata": dict(found.get("metadata") or {})}
        for s3_name, gcs_name in _HEADERS.items():
            if found.get(gcs_name):
                result[s3_name] = found[gcs_name]
        retention = found.get("retention") or {}
        if retention.get("mode"):
            result["ObjectLockMode"] = retention["mode"].upper()
            result["ObjectLockRetainUntilDate"] = retention.get("retainUntilTime")
        if found.get("temporaryHold") or found.get("eventBasedHold"):
            result["ObjectLockLegalHoldStatus"] = "ON"
        return result

    def get_object_tagging(self, Bucket, Key):  # noqa: N803
        self._metadata(Bucket, Key, "GetObjectTagging")
        return {"TagSet": []}  # GCS objects have no tags

    def put_object(self, Bucket, Key, Body=b"", **extra):  # noqa: N803
        data = Body if isinstance(Body, bytes) else Body.read()
        return self._upload(Bucket, Key, [data], extra)

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):  # noqa: N803
        def chunks():
            while True:
                chunk = Fileobj.read(_CHUNK)
                if not chunk:
                    return
                yield chunk
        self._upload(Bucket, Key, chunks(), dict(ExtraArgs or {}))

    def _upload(self, bucket, key, chunks, extra):
        if extra.get("Tagging"):
            raise ContractError("GCS objects have no tags to copy")
        metadata = {"name": key, "metadata": dict(extra.get("Metadata") or {})}
        for s3_name, gcs_name in _HEADERS.items():
            if extra.get(s3_name):
                metadata[gcs_name] = extra[s3_name]
        started = self.rest.session.request(
            "POST", f"{UPLOAD}/b/{bucket}/o", params={"uploadType": "resumable"},
            json=metadata, timeout=self.rest.timeout)
        if started.status_code != 200:
            code = {404: "NoSuchBucket", 403: "AccessDenied"}.get(started.status_code,
                                                                  str(started.status_code))
            raise _error(started.status_code, code, "PutObject")
        session = started.headers["Location"]
        offset, pending = 0, None
        for chunk in chunks:
            if pending is not None:
                self._put_chunk(session, pending, offset, None)
                offset += len(pending)
            pending = chunk
        pending = pending or b""
        return self._put_chunk(session, pending, offset, offset + len(pending))

    def _put_chunk(self, session, chunk, offset, total):
        if total is None:
            span = f"bytes {offset}-{offset + len(chunk) - 1}/*"
        elif chunk:
            span = f"bytes {offset}-{offset + len(chunk) - 1}/{total}"
        else:
            span = f"bytes */{total}"
        response = self.rest.session.request("PUT", session, data=chunk,
                                             headers={"Content-Range": span},
                                             timeout=self.rest.timeout)
        if response.status_code == 308 and total is None:
            return {}
        if response.status_code in (200, 201) and total is not None:
            return {"ETag": response.json().get("etag")}
        code = {403: "AccessDenied", 412: "PreconditionFailed"}.get(response.status_code,
                                                                     str(response.status_code))
        raise _error(response.status_code, code, "PutObject")

    def delete_object(self, Bucket, Key):  # noqa: N803
        try:
            self._call("DeleteObject", "DELETE", self._object_url(Bucket, Key))
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchKey":
                raise  # deleting what is not there is done, as on S3
        return {}

    # -- listings ----------------------------------------------------------------
    def get_paginator(self, name):
        pages = {"list_objects_v2": self._objects, "list_object_versions": self._versions,
                 "list_multipart_uploads": self._uploads}.get(name)
        if pages is None:
            raise ContractError(f"Unsupported listing on GCS: {name}")
        return _Pages(pages)

    def _list(self, bucket, **params):
        token = None
        while True:
            page = self._call("ListObjects", "GET", f"{self._bucket_url(bucket)}/o",
                              missing="NoSuchBucket", params={
                                  "maxResults": 1000, **params,
                                  **({"pageToken": token} if token else {})})
            yield page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return

    def _objects(self, Bucket, Prefix=None):  # noqa: N803
        for items in self._list(Bucket, **({"prefix": Prefix} if Prefix else {})):
            yield {"Contents": [{"Key": o["name"], "Size": int(o.get("size", 0))}
                                for o in items]}

    def _versions(self, Bucket):  # noqa: N803
        for items in self._list(Bucket, versions="true"):
            yield {"Versions": [{"Key": o["name"], "VersionId": o["generation"],
                                 "IsLatest": "timeDeleted" not in o} for o in items],
                   "DeleteMarkers": []}  # GCS keeps no delete markers

    def _uploads(self, Bucket):  # noqa: N803
        """In-progress XML API multipart uploads (the JSON API has none)."""
        marker = {}
        while True:
            response = self.rest.session.request(
                "GET", f"{XML}/{Bucket}", params={"uploads": "", **marker},
                timeout=self.rest.timeout)
            if response.status_code == 404:
                raise _error(404, "NoSuchBucket", "ListMultipartUploads")
            if response.status_code != 200:
                raise _error(response.status_code, "AccessDenied" if response.status_code == 403
                             else str(response.status_code), "ListMultipartUploads")
            root = ElementTree.fromstring(response.content)
            local = {e.tag.rsplit("}", 1)[-1]: e for e in root}
            uploads = [{"Key": next(c.text for c in u if c.tag.endswith("Key")),
                        "UploadId": next(c.text for c in u if c.tag.endswith("UploadId"))}
                       for u in root if u.tag.endswith("Upload")]
            yield {"Uploads": uploads}
            truncated = local.get("IsTruncated")
            if truncated is None or truncated.text != "true":
                return
            marker = {"key-marker": local["NextKeyMarker"].text,
                      "upload-id-marker": local["NextUploadIdMarker"].text}


def _bindings_mark(bindings):
    return hashlib.sha256(canonical_json({"bindings": sorted(
        ({"role": b["role"], "members": sorted(b.get("members", [])),
          "condition": b.get("condition")} for b in bindings or ()),
        key=lambda b: (b["role"], b["members"]))}, limit=1 << 20).encode()).hexdigest()[:16]


class GcsBucketIamAccess:
    def __init__(self, rest, bucket, *, principals, propagation_seconds=420,
                 clock=time.time, sleep=time.sleep):
        """``principals`` maps logical writer names to IAM members
        (``serviceAccount:...``, ``user:...``, ``group:...``)."""
        if not isinstance(rest, GcpRest):
            raise ContractError("GCP REST access required")
        if not _BUCKET.fullmatch(bucket or ""):
            raise ContractError("GCS bucket name required")
        if type(principals) is not dict or not principals or not all(
                type(k) is str and type(v) is str and re.fullmatch(
                    r"(serviceAccount|user|group):[^\s,]+", v) for k, v in principals.items()):
            raise ContractError("Logical writer names mapped to IAM members required")
        if type(propagation_seconds) not in (int, float) or not 0 <= propagation_seconds <= 3600:
            raise ContractError("Bounded IAM propagation window required")
        self.rest, self.bucket, self.principals = rest, bucket, dict(principals)
        self.url = f"{STORAGE}/b/{bucket}"
        self.propagation_seconds, self.clock, self.sleep = propagation_seconds, clock, sleep

    def _p(self, name):
        if name not in self.principals:
            raise ContractError("Writer has no principal on this store")
        return self.principals[name]

    def _policy(self):
        return self.rest.get(self.url + "/iam", params={"optionsRequestedPolicyVersion": 3},
                             missing_ok=True)

    def _labels(self):
        found = self.rest.get(self.url, missing_ok=True)
        return None if found is None else (found.get("labels") or {})

    @staticmethod
    def _marker(name):
        if not re.fullmatch(r"[A-Za-z0-9]{1,40}", name or ""):
            raise ContractError("Alphanumeric statement name required")
        return MARKER + name.lower()

    def _store(self, policy):
        policy["version"] = 3
        try:
            self.rest.call("PUT", self.url + "/iam", json=policy)
        except GcpApiError as error:
            if error.status in (409, 412):
                raise ContractError("The bucket policy changed in between") from None
            raise

    def _bucket(self):
        return self.rest.get(self.url, missing_ok=True)

    def _record(self, bindings, changed, extra=None):
        """Mark the handoff's policy change (or adopt one made outside it, whose
        time is unknown) and wait until it can have reached every front end."""
        bucket = self._bucket()
        if bucket is None:
            raise ContractError("Bucket does not exist")
        labels, generation = bucket.get("labels") or {}, int(bucket["metageneration"])
        mark = _bindings_mark(bindings)
        untouched = (labels.get(METAGENERATION) == str(generation)
                     and labels.get(BINDINGS) == mark)
        wanted = dict(extra or {})
        if changed or not untouched:
            wanted[CHANGED_AT] = str(int(self.clock()))
        if any(labels.get(k) != v for k, v in wanted.items()) or not untouched:
            wanted.update({BINDINGS: mark, METAGENERATION: str(generation + 1)})
            try:
                self.rest.patch(self.url, {"labels": wanted},
                                params={"ifMetagenerationMatch": str(generation)})
            except GcpApiError as error:
                if error.status == 412:
                    raise ContractError("The bucket changed in between") from None
                raise
        at = int(wanted.get(CHANGED_AT, labels.get(CHANGED_AT, 0)))
        remaining = at + self.propagation_seconds - self.clock()
        if remaining > 0:
            self.sleep(remaining)

    def _settled(self, bucket, policy):
        labels = bucket.get("labels") or {}
        if labels.get(METAGENERATION) is None:
            return True  # the handoff never changed this bucket: nothing of its own pending
        return (labels[METAGENERATION] == str(bucket.get("metageneration"))
                and labels.get(BINDINGS) == _bindings_mark(policy.get("bindings"))
                and self.clock() - int(labels.get(CHANGED_AT, 0)) >= self.propagation_seconds)

    # -- BucketAccess --------------------------------------------------------------
    def deny(self, name, principals):
        if not principals:
            raise ContractError("Principals required")
        marker = self._marker(name)
        members = {self._p(p) for p in principals}
        policy = self._policy()
        if policy is None:
            raise ContractError("Bucket does not exist")
        kept = []
        for binding in policy.get("bindings", []):
            remaining = [m for m in binding.get("members", []) if m not in members]
            if remaining:
                kept.append({**binding, "members": remaining})
        changed = kept != policy.get("bindings", [])
        if changed:
            self._store({**policy, "bindings": kept})
        self._record(kept, changed, {marker: "deny"})

    def open_writer(self, principal):
        member = self._p(principal)
        policy = self._policy()
        if policy is None:
            raise ContractError("Bucket does not exist")
        bindings = policy.get("bindings", [])
        target = next((b for b in bindings if b["role"] == WRITER_ROLE and "condition" not in b),
                      None)
        changed = True
        if target is None:
            bindings.append({"role": WRITER_ROLE, "members": [member]})
        elif member not in target["members"]:
            target["members"].append(member)
        else:
            changed = False
        if changed:
            self._store({**policy, "bindings": bindings})
        self._record(bindings, changed)

    def _access(self, member, permission):
        kind, _, identity = member.partition(":")
        answer = self.rest.post(TROUBLESHOOT, {"accessTuple": {
            "principal": identity,
            "fullResourceName": f"//storage.googleapis.com/projects/_/buckets/{self.bucket}",
            "permission": permission}})
        return answer.get("overallAccessState", "UNKNOWN")

    def denied(self, principals, *, by=None):
        """A denial counts only where the handoff has denied (its marker, of
        ``by`` when named): with allow-only IAM a principal nobody granted
        anything is also unable to write, and that incidental state is not the
        handoff's postcondition (a pre-created empty bucket is not staged)."""
        names = {p: self._p(p) for p in principals}  # an unknown writer is an error
        bucket, policy = self._bucket(), self._policy()
        labels = (bucket or {}).get("labels") or {}
        markers = {k for k in labels if k.startswith(MARKER) and k not in _BOOKKEEPING}
        if bucket is None or policy is None or not self._settled(bucket, policy) or (
                self._marker(by) not in markers if by is not None else not markers):
            return dict.fromkeys(names, False)
        return {p: all(self._access(member, permission) == "CANNOT_ACCESS"
                       for permission in _WRITE) for p, member in names.items()}

    def writable_by(self, principal):
        member = self._p(principal)
        bucket, policy = self._bucket(), self._policy()
        if bucket is None or policy is None or not self._settled(bucket, policy):
            return False
        return all(self._access(member, permission) == "CAN_ACCESS" for permission in _WRITE)

    def lift(self, names):
        """Remove the handoff's markers (the bindings are the owner's to restore)."""
        self.rest.patch(self.url, {"labels": {self._marker(n): None for n in names}})

    def policy_text(self):
        policy, labels = self._policy(), self._labels()
        if policy is None:
            return None
        return canonical_json({"bindings": sorted(
            ({"role": b["role"], "members": sorted(b.get("members", [])),
              **({"condition": b["condition"]} if "condition" in b else {})}
             for b in policy.get("bindings", [])), key=lambda b: (b["role"], b["members"])),
            "markers": sorted(k for k in labels if k.startswith(MARKER)
                              and k not in _BOOKKEEPING)}, limit=65536)
