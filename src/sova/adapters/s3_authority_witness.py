"""Authority witness on object storage with an Object Lock retention (AT-33, §15.6).

The file witness can tell an edited entry but not a cut tail. Here every
witnessed authority record is its own object, written once (``If-None-Match``)
with an Object Lock retention, in a bucket under a permission boundary apart
from the authority database. Retained versions cannot be deleted or rewritten
until the retention ends, even by the bucket owner in compliance mode.

What remains possible is hiding: a delete without a version id leaves a delete
marker and the entry disappears from ordinary listings; an unconditional PUT
adds a second version of a key. The witness therefore reads the version
listing, never the ordinary one, and refuses to answer (fails closed) when it
finds a delete marker, a key with more than one version, a gap in the
sequence, a broken hash chain, an entry without a lock, or a latest entry
whose lock has already expired. The authority store then stops, which is the
intended reaction to tampering with the evidence that guards it.

Entries for one (application, unit) form a chain: each carries its sequence
number and the SHA-256 of the previous entry's bytes. The port is the file
witness's::

    append(application_id, unit_digest, action, document) -> None
    latest(application_id, unit_digest) -> dict | None
"""

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json

from botocore.exceptions import ClientError

from sova.domain.contracts import ContractError, canonical_json, digest, identifier

_GENESIS = "0" * 64
_MODES = ("COMPLIANCE", "GOVERNANCE")


class S3ObjectLockAuthorityWitness:
    def __init__(self, client, bucket, *, tenant_id, retention, mode="COMPLIANCE",
                 max_entries=10_000, clock=lambda: datetime.now(UTC)):
        if mode not in _MODES:
            raise ContractError("Object Lock mode required")
        if not isinstance(retention, timedelta) or retention < timedelta(hours=1):
            raise ContractError("An explicit retention of at least an hour required")
        if type(max_entries) is not int or not 1 <= max_entries <= 100_000:
            raise ContractError("Bounded witness length required")
        self.client, self.bucket = client, bucket
        self.tenant_id = identifier(tenant_id, "tenant_id")
        self.retention, self.mode = retention, mode
        self.max_entries, self.clock = max_entries, clock

    def require_locked_bucket(self):
        """The bucket must enforce Object Lock; a plain bucket is not a witness."""
        try:
            config = self.client.get_object_lock_configuration(Bucket=self.bucket)
        except ClientError as error:
            raise ContractError("Witness bucket without Object Lock") from error
        if config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") != "Enabled":
            raise ContractError("Witness bucket without Object Lock")

    def _prefix(self, application_id, unit_digest):
        identifier(application_id)
        digest(unit_digest)
        return f"authority-witness/{self.tenant_id}/{application_id}/{unit_digest}/"

    def _chain(self, prefix):
        """Verified entries, oldest first, as (bytes, entry); fails closed."""
        versions, markers = [], 0
        pages = self.client.get_paginator("list_object_versions").paginate(
            Bucket=self.bucket, Prefix=prefix)
        for page in pages:
            versions += page.get("Versions", ())
            markers += len(page.get("DeleteMarkers", ()))
            if len(versions) > self.max_entries:
                raise ContractError("Authority witness longer than its bound")
        if markers:
            raise ContractError("Authority witness entry was hidden by a delete marker")
        keys = [v["Key"] for v in versions]
        if len(set(keys)) != len(keys):
            raise ContractError("Authority witness entry was rewritten")
        expected = [f"{prefix}{n:012d}.json" for n in range(1, len(keys) + 1)]
        if sorted(keys) != expected:
            raise ContractError("Authority witness sequence has a gap")
        by_key = {v["Key"]: v["VersionId"] for v in versions}
        chain, previous = [], _GENESIS
        for n, key in enumerate(expected, start=1):
            response = self.client.get_object(Bucket=self.bucket, Key=key,
                                              VersionId=by_key[key])
            body = response["Body"].read()
            entry = json.loads(body)
            if (entry.get("seq"), entry.get("prev"), entry.get("tenant_id")) != (
                    n, previous, self.tenant_id):
                raise ContractError("Authority witness chain is broken")
            if response.get("ObjectLockMode") not in _MODES or not response.get(
                    "ObjectLockRetainUntilDate"):
                raise ContractError("Authority witness entry is not locked")
            chain.append((entry, response["ObjectLockRetainUntilDate"]))
            previous = hashlib.sha256(body).hexdigest()
        if chain and chain[-1][1] <= self.clock():
            raise ContractError("Latest authority witness entry is no longer locked")
        return chain, previous

    def append(self, application_id, unit_digest, action, document):
        prefix = self._prefix(application_id, unit_digest)
        identifier(action, "action", limit=512)
        if type(document) is not dict:
            raise ContractError("Authority record document required")
        chain, previous = self._chain(prefix)
        seq = len(chain) + 1
        body = canonical_json({
            "tenant_id": self.tenant_id, "application_id": application_id,
            "unit_digest": unit_digest, "action": action, "record": document,
            "seq": seq, "prev": previous}, limit=262144).encode()
        until = self.clock() + self.retention
        key = f"{prefix}{seq:012d}.json"
        try:
            put = self.client.put_object(
                Bucket=self.bucket, Key=key, Body=body, IfNoneMatch="*",
                ContentType="application/json", ObjectLockMode=self.mode,
                ObjectLockRetainUntilDate=until,
                ChecksumSHA256=base64.b64encode(hashlib.sha256(body).digest()).decode())
        except ClientError as error:
            # Someone else appended this sequence number first: never overwrite.
            raise ContractError("Authority witness append refused") from error
        # Readback: the stored version carries the lock that was asked for.
        head = self.client.head_object(Bucket=self.bucket, Key=key, VersionId=put["VersionId"])
        if head.get("ObjectLockMode") != self.mode or head.get(
                "ObjectLockRetainUntilDate") is None or head["ObjectLockRetainUntilDate"] < (
                until - timedelta(seconds=1)):
            raise ContractError("Authority witness entry stored without its lock")

    def latest(self, application_id, unit_digest):
        chain, _ = self._chain(self._prefix(application_id, unit_digest))
        return chain[-1][0]["record"] if chain else None
