"""Reference adapter: offline copy of one object-storage bucket to another.

The two buckets may belong to different providers. Each side has its own S3
API client (any S3-compatible endpoint, with its own credentials) and its own
``BucketAccess`` for the provider's access model, so the data path does not
depend on who holds either bucket. Every postcondition is read back from the
stores themselves:

* stage_target - the target bucket exists with the source's versioning and
  object-lock state, and every registered writer is denied on it;
* verify_return_target (failback) - the original source still exists, every
  writer is denied there, and no multipart upload is in progress on it;
* start_sync - an initial copy while the source still accepts writes, marked
  by a tag on the target bucket;
* pause_admission - every registered writer is denied every request on the
  source;
* fence_source_writes - the fence's own denial covers every writer and no
  multipart upload is in progress on the source. An upload started before
  the fence may hold business data the client still means to complete: the
  operator completes or aborts it, the adapter never discards it;
* copy_content / apply_final_changes - every current object of the source is
  on the target with the same bytes (SHA-256), content headers, user metadata
  and tags; target objects the source does not have are removed;
* verify_final_state - the same comparison, and the source holds no version
  history (noncurrent versions, delete markers) that a current-state copy
  would lose, unless the adapter was built to accept that loss;
* activate_target - the application may write the target, every other writer
  stays denied there, and the source stays denied;
* commit_route - the route points at the target bucket.

Limits of this reference: current versions only; object-lock retention and
legal holds are compared but not copied, so a source using them never
verifies; server-side encryption follows each store's default and is not
compared; object ACLs are not copied (access is the bucket's, through
``BucketAccess``); every comparison reads each object through this process,
bounded by ``max_objects``.
"""

import hashlib
from urllib.parse import urlencode

from botocore.exceptions import ClientError

from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stage_target", "verify_return_target", "start_sync", "pause_admission",
                     "fence_source_writes", "copy_content", "apply_final_changes",
                     "verify_final_state", "activate_target", "commit_route"})
SYNC_TAG = "sova-handoff-sync"
_HEADERS = ("ContentType", "CacheControl", "ContentDisposition", "ContentEncoding",
            "ContentLanguage")
_LOCK = ("ObjectLockMode", "ObjectLockRetainUntilDate", "ObjectLockLegalHoldStatus")
_CHUNK = 1 << 20


class _TooMany(ContractError):
    """More objects than this adapter is bounded to read."""


def _code(error):
    return error.response.get("Error", {}).get("Code", "")


class S3OfflineCopyAdapter:
    def __init__(self, adapter_id, *, source, target, app_principal, writer_principals, route,
                 max_objects=10_000, accept_history_loss=False):
        """``source`` and ``target`` are ``(client, bucket, access)`` triples."""
        for side in (source, target):
            if type(side) is not tuple or len(side) != 3:
                raise ContractError("(client, bucket, access) required for each side")
        if not all(callable(getattr(route, n, None)) for n in ("get", "set")):
            raise ContractError("A route store required")
        writers = tuple(writer_principals or ())
        if not writers or app_principal not in writers:
            raise ContractError("The application must be a registered writer")
        if type(max_objects) is not int or not 1 <= max_objects <= 1_000_000:
            raise ContractError("Bounded object count required")
        self.adapter_id, self.route = adapter_id, route
        self.sides = {"source": source, "target": target}
        self.app, self.writers = app_principal, writers
        self.max_objects, self.accept_history_loss = max_objects, accept_history_loss

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported handoff action for this adapter")

    def apply(self, command):
        getattr(self, "_" + command.action)()

    def readback(self, command, now):
        try:
            holds, detail = getattr(self, "_check_" + command.action)()
        except _TooMany:
            holds, detail = False, {"reason": "object bound exceeded",
                                    "max_objects": self.max_objects}
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if holds else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))

    # -- reading the stores --------------------------------------------------
    def _client(self, side):
        return self.sides[side][0]

    def _bucket(self, side):
        return self.sides[side][1]

    def _access(self, side):
        return self.sides[side][2]

    def _ref(self, side):
        return f"{self._client(side).meta.endpoint_url}/{self._bucket(side)}"

    def _exists(self, side):
        try:
            self._client(side).head_bucket(Bucket=self._bucket(side))
            return True
        except ClientError as error:
            if _code(error) in ("404", "NoSuchBucket", "NotFound"):
                return False
            raise

    def _versioning(self, side):
        status = self._client(side).get_bucket_versioning(Bucket=self._bucket(side))
        return status.get("Status", "Disabled")

    def _object_lock(self, side):
        try:
            config = self._client(side).get_object_lock_configuration(
                Bucket=self._bucket(side))
        except ClientError as error:
            if _code(error) == "ObjectLockConfigurationNotFoundError":
                return "Disabled"
            raise
        return config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled", "Disabled")

    def _keys(self, side):
        keys = []
        pages = self._client(side).get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket(side))
        for page in pages:
            keys += [o["Key"] for o in page.get("Contents", ())]
            if len(keys) > self.max_objects:
                raise _TooMany
        return keys

    def _object(self, side, key):
        client, bucket = self._client(side), self._bucket(side)
        response = client.get_object(Bucket=bucket, Key=key)
        h, size = hashlib.sha256(), 0
        for chunk in response["Body"].iter_chunks(_CHUNK):
            h.update(chunk)
            size += len(chunk)
        tags = client.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", ())
        return {
            "sha256": h.hexdigest(), "size": size,
            **{name: response.get(name) for name in _HEADERS},
            "metadata": dict(sorted(response.get("Metadata", {}).items())),
            "tags": sorted([t["Key"], t["Value"]] for t in tags),
            **{name: str(response[name]) for name in _LOCK if name in response},
        }

    def _inventory(self, side):
        """Key -> fingerprint of every current object; None if the bucket is gone."""
        if not self._exists(side):
            return None
        return {key: self._object(side, key) for key in self._keys(side)}

    def _uploads(self, side):
        if not self._exists(side):
            return []
        pages = self._client(side).get_paginator("list_multipart_uploads").paginate(
            Bucket=self._bucket(side))
        return sorted(u["Key"] for page in pages for u in page.get("Uploads", ()))

    def _history(self, side):
        """Noncurrent versions and delete markers a current-state copy loses."""
        if self._versioning(side) == "Disabled":
            return {"noncurrent_versions": 0, "delete_markers": 0}
        noncurrent = markers = 0
        pages = self._client(side).get_paginator("list_object_versions").paginate(
            Bucket=self._bucket(side))
        for page in pages:
            noncurrent += sum(1 for v in page.get("Versions", ()) if not v["IsLatest"])
            markers += len(page.get("DeleteMarkers", ()))
        return {"noncurrent_versions": noncurrent, "delete_markers": markers}

    def _sync_tags(self):
        try:
            tags = self._client("target").get_bucket_tagging(
                Bucket=self._bucket("target")).get("TagSet", [])
        except ClientError as error:
            if _code(error) in ("NoSuchTagSet", "NoSuchBucket"):
                return []
            raise
        return list(tags)

    def _compare(self):
        source, target = self._inventory("source"), self._inventory("target")
        if source is None or target is None:
            return False, {"source_present": source is not None,
                           "target_present": target is not None}
        differing = sorted(k for k in set(source) | set(target)
                           if source.get(k) != target.get(k))
        digest = {side: hashlib.sha256(canonical_json(
            {"objects": inventory}, limit=1 << 30).encode()).hexdigest()
            for side, inventory in (("source", source), ("target", target))}
        return not differing, {"objects": [len(source), len(target)], "sha256": digest,
                               "differing": len(differing), "first_differing": differing[:20]}

    # -- actions ---------------------------------------------------------------
    def _stage_target(self):
        client, bucket = self._client("target"), self._bucket("target")
        if not self._exists("target"):
            args = {"Bucket": bucket}
            region = client.meta.region_name
            if region and region != "us-east-1":
                args["CreateBucketConfiguration"] = {"LocationConstraint": region}
            if self._object_lock("source") == "Enabled":
                args["ObjectLockEnabledForBucket"] = True
            try:
                client.create_bucket(**args)
            except ClientError as error:
                if _code(error) != "BucketAlreadyOwnedByYou":
                    raise
        wanted = self._versioning("source")
        if wanted != "Disabled" and self._versioning("target") != wanted:
            client.put_bucket_versioning(Bucket=bucket,
                                         VersioningConfiguration={"Status": wanted})
        self._access("target").deny("TargetNoAccess", self.writers)

    def _check_stage_target(self):
        if not self._exists("target"):
            return False, {"target_present": False}
        config = {side: {"versioning": self._versioning(side),
                         "object_lock": self._object_lock(side)}
                  for side in ("source", "target")}
        denied = self._access("target").denied(self.writers)
        return config["source"] == config["target"] and all(denied.values()), {
            "target_present": True, "configuration": config, "target_writers_denied": denied}

    def _verify_return_target(self):
        return None  # verification only: the readback is the check

    def _check_verify_return_target(self):
        staged, detail = self._check_stage_target()
        uploads = self._uploads("target")
        return staged and not uploads, {**detail, "uploads_in_progress": uploads[:20]}

    def _start_sync(self):
        self._copy()
        tags = [t for t in self._sync_tags() if t["Key"] != SYNC_TAG]
        self._client("target").put_bucket_tagging(
            Bucket=self._bucket("target"),
            Tagging={"TagSet": tags + [{"Key": SYNC_TAG, "Value": "initial-copy"}]})

    def _check_start_sync(self):
        marked = any(t["Key"] == SYNC_TAG for t in self._sync_tags())
        return marked, {"sync_marked": marked}

    def _pause_admission(self):
        self._access("source").deny("PauseAdmission", self.writers)

    def _check_pause_admission(self):
        denied = self._access("source").denied(self.writers)
        return all(denied.values()), {"source_writers_denied": denied}

    def _fence_source_writes(self):
        self._access("source").deny("FenceWrites", self.writers)

    def _check_fence_source_writes(self):
        denied = self._access("source").denied(self.writers, by="FenceWrites")
        uploads = self._uploads("source")
        return all(denied.values()) and not uploads, {
            "source_writers_fenced": denied, "uploads_in_progress": uploads[:20]}

    def _copy(self):
        source, target = self._inventory("source"), self._inventory("target")
        if source is None or target is None:
            raise ContractError("Both buckets must exist to copy")
        client, bucket = self._client("target"), self._bucket("target")
        for key, fingerprint in sorted(source.items()):
            if target.get(key) == fingerprint:
                continue
            response = self._client("source").get_object(Bucket=self._bucket("source"),
                                                          Key=key)
            extra = {name: response[name] for name in _HEADERS if response.get(name)}
            extra["Metadata"] = response.get("Metadata", {})
            if fingerprint["tags"]:
                extra["Tagging"] = urlencode([tuple(t) for t in fingerprint["tags"]])
            # A new PUT replaces the object's tags too, so stale target tags go.
            client.upload_fileobj(response["Body"], bucket, key, ExtraArgs=extra)
        for key in sorted(set(target) - set(source)):
            client.delete_object(Bucket=bucket, Key=key)

    def _copy_content(self):
        self._copy()

    def _check_copy_content(self):
        return self._compare()

    def _apply_final_changes(self):
        self._copy()

    def _check_apply_final_changes(self):
        return self._compare()

    def _verify_final_state(self):
        return None  # measurement only; the readback is the comparison

    def _check_verify_final_state(self):
        equal, detail = self._compare()
        history = self._history("source") if self._exists("source") else {}
        kept = self.accept_history_loss or not any(history.values())
        return equal and kept, {**detail, "source_history": history,
                                "history_loss_accepted": self.accept_history_loss}

    def _activate_target(self):
        self._access("target").open_writer(self.app)

    def _check_activate_target(self):
        target = self._access("target")
        writable = target.writable_by(self.app)
        others = target.denied(tuple(w for w in self.writers if w != self.app))
        source = self._access("source").denied(self.writers)
        return writable and all(others.values()) and all(source.values()), {
            "target_writable_by_application": writable, "target_others_denied": others,
            "source_writers_denied": source}

    def _commit_route(self):
        self.route.set(self._ref("target"))

    def _check_commit_route(self):
        current = self.route.get()
        return current == self._ref("target"), {"route": current}
