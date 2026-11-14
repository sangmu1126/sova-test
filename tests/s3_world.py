"""S3 handoff world shared by the local and the AWS object-storage tests.

Writers are logical ("app", "other"); ``stores`` supplies each side's clients,
principal names, bucket naming and bucket creation.
"""

import hashlib
import json
from uuid import uuid4

from botocore.exceptions import ClientError

from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.s3_offline_copy import S3OfflineCopyAdapter

LARGE = 9 * 1024 * 1024  # above the default multipart threshold of the copy


class Route:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def application_access(bucket, principals):
    """The customer's own policy before any handoff: both writers may work."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "ApplicationAccess", "Effect": "Allow", "Principal": {"AWS": principals},
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
                   "s3:GetObjectTagging", "s3:PutObjectTagging",
                   "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


class S3World:
    def __init__(self, stores, *, versioned=False, accept_history_loss=False):
        self.s = stores
        suffix = uuid4().hex[:10]
        self.buckets = {side: stores["bucket_name"](side, suffix)
                        for side in ("source", "target")}
        self.root = {side: stores["root"](side) for side in ("source", "target")}
        # Writers are logical ("app", "other"); each store has its own principal names.
        self.principals = stores["principals"]
        self.accept_history_loss = accept_history_loss
        source, bucket = self.root["source"], self.buckets["source"]
        stores["create_bucket"](source, bucket)
        if versioned:
            source.put_bucket_versioning(Bucket=bucket,
                                         VersioningConfiguration={"Status": "Enabled"})
        self.base_policy = application_access(
            bucket, sorted(self.principals["source"].values()))
        source.put_bucket_policy(Bucket=bucket, Policy=json.dumps(self.base_policy))
        app = stores["as"]("source", "app")
        for i in range(1, 11):
            app.put_object(Bucket=bucket, Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i, "amount": i * 10}).encode(),
                           ContentType="application/json", Metadata={"customer": f"c-{i % 3}"},
                           **({"Tagging": "class=invoice"} if i % 4 == 0 else {}))
        app.upload_fileobj(_Bytes(b"x" * LARGE), bucket, "exports/large.bin",
                           ExtraArgs={"ContentType": "application/octet-stream"})
        self.next_order = 11
        self._route = Route(self.route_of("source"))
        self.pending = None

    def side(self, name):
        """(client, bucket, access) for the adapter, with this store's principals."""
        client = self.s["root"](name)
        return client, self.buckets[name], S3BucketPolicyAccess(
            client, self.buckets[name], principals=self.principals[name])

    def _adapter(self, source, target):
        return S3OfflineCopyAdapter(
            "s3-offline-copy", source=self.side(source), target=self.side(target),
            app_principal="app", writer_principals=("app", "other"), route=self._route,
            accept_history_loss=self.accept_history_loss)

    def adapter(self):
        return self._adapter("source", "target")

    def reverse_adapter(self):
        return self._adapter("target", "source")

    def _exists(self, side):
        try:
            self.root[side].head_bucket(Bucket=self.buckets[side])
            return True
        except ClientError:
            return False

    def can_write(self, side, writer):
        """A conditional write that can never succeed: If-Match with an ETag no
        object has, on a key that does not exist. An allowed writer gets "no
        such key" (or a failed precondition), a denied one an access denial;
        nothing is ever written."""
        if not self._exists(side):
            return False
        try:
            self.s["as"](side, writer).put_object(
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
        app = self.s["as"]("source", "app")
        for i in range(self.next_order, self.next_order + count):
            app.put_object(Bucket=self.buckets["source"], Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i}).encode(),
                           ContentType="application/json")
        self.next_order += count

    def business_delete(self, count):
        app, bucket = self.s["as"]("source", "app"), self.buckets["source"]
        listed = app.list_objects_v2(Bucket=bucket, Prefix="orders/", MaxKeys=count)
        for item in listed.get("Contents", ()):
            app.delete_object(Bucket=bucket, Key=item["Key"])

    def content(self, side):
        if not self._exists(side):
            return None
        client, bucket, h = self.root[side], self.buckets[side], hashlib.sha256()
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for key in sorted(o["Key"] for p in pages for o in p.get("Contents", ())):
            r = client.get_object(Bucket=bucket, Key=key)
            tags = client.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
            h.update(json.dumps([key, hashlib.sha256(r["Body"].read()).hexdigest(),
                                 r.get("ContentType"), r.get("Metadata"),
                                 sorted((t["Key"], t["Value"]) for t in tags)]).encode())
        return h.hexdigest()

    def statements(self, side):
        """The current policy statements by Sid, lists compared as sets."""
        document = json.loads(self.root[side].get_bucket_policy(
            Bucket=self.buckets[side])["Policy"])
        statements = document.get("Statement", [])
        statements = [statements] if isinstance(statements, dict) else statements
        return {s["Sid"]: canonical(s) for s in statements}

    def _policy(self, side):
        """The policy as a set-valued document: AWS returns principal and
        action lists in a different order from one read to the next."""
        try:
            document = json.loads(self.root[side].get_bucket_policy(
                Bucket=self.buckets[side])["Policy"])
        except ClientError as error:
            return error.response["Error"]["Code"]
        statements = document.get("Statement", [])
        statements = [statements] if isinstance(statements, dict) else statements
        return sorted((json.dumps(canonical(s), sort_keys=True) for s in statements))

    def _uploads(self, side):
        if not self._exists(side):
            return None
        return sorted(u["Key"] for u in self.root[side].list_multipart_uploads(
            Bucket=self.buckets[side]).get("Uploads", ()))

    def state(self):
        return tuple((self.content(side), json.dumps(self._policy(side), sort_keys=True),
                      self._uploads(side)) for side in ("source", "target")) + (
            self._route.get(),)

    def reopen_source(self):
        """The source policy restored from before the handoff."""
        self.root["source"].put_bucket_policy(Bucket=self.buckets["source"],
                                              Policy=json.dumps(self.base_policy))

    def tamper_target(self):
        self.root["target"].put_object(Bucket=self.buckets["target"],
                                       Key="orders/0001.json", Body=b'{"order": 1, "x": 1}',
                                       ContentType="application/json",
                                       Metadata={"customer": "c-1"})

    def move_route(self, side):
        self._route.set(self.route_of(side))

    def route(self):
        return self._route.get()

    def route_of(self, side):
        return f"{self.root[side].meta.endpoint_url}/{self.buckets[side]}"

    def open_pending_write(self):
        app = self.s["as"]("source", "app")
        upload = app.create_multipart_upload(Bucket=self.buckets["source"],
                                             Key="exports/pending.bin")
        app.upload_part(Bucket=self.buckets["source"], Key="exports/pending.bin",
                        UploadId=upload["UploadId"], PartNumber=1, Body=b"p" * 1024)
        self.pending = upload["UploadId"]

    def resolve_pending_write(self):
        self.root["source"].abort_multipart_upload(Bucket=self.buckets["source"],
                                                   Key="exports/pending.bin",
                                                   UploadId=self.pending)


class _Bytes:
    """A minimal readable stream, so the seed upload itself goes multipart."""

    def __init__(self, data):
        self.data, self.at = data, 0

    def read(self, size=-1):
        end = len(self.data) if size is None or size < 0 else self.at + size
        chunk, self.at = self.data[self.at:end], min(end, len(self.data))
        return chunk
