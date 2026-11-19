"""Independent probes for an object-storage unit (§16, §15.5, §22.1, §28.2).

Made apart from the adapter that copied the objects, through the S3 API of
each side (any S3-compatible store). Every decision is the store's own answer
to a request; nothing is concluded from configuration alone.

* Write denial needs two views to agree (ADR-0007): an attempt - a
  conditional write as each registered writer that can never succeed
  (``If-Match`` an ETag no object has, on a key that does not exist), so an
  allowed writer is told the object is missing and a denied one is refused -
  and the access model's own view (``BucketAccess.denied``). A store can
  keep a denial it does not enforce: SeaweedFS 4.52 accepts, stores and
  returns a bucket policy naming a principal it does not resolve, and the
  writer keeps writing (measured). The attempt catches it; the policy alone
  would not.
* Single writer after activation: the application may write the target (both
  views), every other writer is refused there, every writer on the source.
* Each protected set is a key prefix, compared object by object: SHA-256 of
  the bytes, size, content headers, user metadata and tags. An empty set on
  the source does not pass: a wrong prefix would compare nothing.
* A business invariant is a registered ``ObjectReference``: every JSON object
  under a prefix that carries the named fields points, through a key
  template, at an object that exists, and no two of them carry the same
  values (an order's result is recorded, once).
* The restore drill copies every object of the target's protected sets into
  a new scratch bucket of the target store, compares the sets there, and
  removes the scratch bucket whatever happens.
* Anonymous access (IAM-01.1) and another tenant's access (ISO-01.1) are
  bracketed by positive controls, as the team's control runner judged them.
  Access can differ by prefix, so each protected set has a witness: an object
  under its prefix, or - before the data is copied - a canary the probe's own
  client places there and removes. The probe's reader reads every witness
  before and after (the application itself is fenced off the target until
  activation), the other tenant reads one of its own objects; between them
  the unsigned client - or the other tenant - reads each witness, lists each
  prefix and tries a conditional write under it, and the application tries
  the other tenant's bucket. Every attempt must be refused with an access
  denial; a credential the store does not recognise is never a refusal. A
  refusal without the positive controls proves nothing (a missing object is
  refused too), so it is undecided; an attempt that is served decides the
  failure.
* Transport: ``transport_encrypted`` is this probe's own connection to the
  side - an HTTPS endpoint, a handshake verified against the registered CA
  and name; ``transport_refusals`` asks the endpoint to refuse a wrong CA, a
  wrong host name and plain HTTP (``sova.adapters.tls_refusal``).
* Placement comes from ``zone_of(side)``, the inventory's view, as read.

Each probe returns ``(passed, observation)``. Observations name principals by
role, never by credential, and carry digests, never object contents. A probe
write is a conditional write that cannot succeed on a store honouring
``If-Match``; on one that ignores it, the empty object written is removed at
once and the attempt counts as allowed.
"""

from dataclasses import dataclass, field
import hashlib
import json
import string
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from sova.adapters.tls_refusal import TlsEndpoint, handshake, http_plaintext, transport_refusals
from sova.domain.contracts import ContractError

NEVER_ETAG = '"00000000000000000000000000000000"'
PROBE_PREFIX = "sova-probe/"
DENIALS = frozenset({"AccessDenied", "AllAccessDisabled"})
_HEADERS = ("ContentType", "CacheControl", "ContentDisposition", "ContentEncoding",
            "ContentLanguage")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class ObjectStoreSide:
    """One side of the unit as the probes reach it.

    ``client`` reads and restores (the operator's reader); ``client_as(name)``
    is a client signed as the logical principal ``name``; ``anonymous`` an
    unsigned client of the same endpoint, when anonymous access is checked;
    ``tls`` the endpoint's registered TLS identity, when transport is."""

    client: object = field(repr=False)
    bucket: str
    access: object = field(repr=False)
    client_as: object = field(repr=False)
    anonymous: object = field(default=None, repr=False)
    tls: TlsEndpoint | None = None


@dataclass(frozen=True)
class ObjectTenant:
    """Another tenant on the target store: a client signed as that tenant, its
    own bucket, and one object known to be there (its positive control)."""

    client: object = field(repr=False)
    bucket: str
    key: str


@dataclass(frozen=True)
class ObjectReference:
    """Every JSON object under ``prefix`` that has all of ``fields`` names the
    object ``template.format(**fields)``, which must exist; no two such
    objects carry the same field values."""

    prefix: str
    template: str
    fields: tuple[str, ...]

    def __post_init__(self):
        named = {n for _, n, _, _ in string.Formatter().parse(self.template) if n}
        if not self.prefix or not self.fields or named != set(self.fields):
            raise ContractError("A prefix and a key template naming exactly its fields required")


def _status(error):
    response = getattr(error, "response", None) or {}
    return (response.get("ResponseMetadata") or {}).get("HTTPStatusCode"), \
        (response.get("Error") or {}).get("Code")


def _refusal(error):
    """'refused' for an access denial; 'undecided:<why>' for anything else."""
    if isinstance(error, ClientError):
        status, code = _status(error)
        if status == 403 and code in DENIALS:
            return "refused"
        return f"undecided:{code or status}"
    return f"undecided:{type(error).__name__}"


class ObjectStoreProbes:
    def __init__(self, *, sides, writers, app, sets, invariants=None, outsider=None,
                 zone_of=None, max_objects=10_000):
        if set(sides) != {"source", "target"} or not all(
                isinstance(s, ObjectStoreSide) for s in sides.values()):
            raise ContractError("An ObjectStoreSide for the source and the target required")
        writers = tuple(writers or ())
        if app not in writers:
            raise ContractError("The application must be a registered writer")
        if type(sets) is not dict or not sets or not all(
                type(p) is str and p for p in sets.values()):
            raise ContractError("A key prefix for each protected set required")
        if outsider is not None and not isinstance(outsider, ObjectTenant):
            raise ContractError("Another tenant is an ObjectTenant")
        if type(max_objects) is not int or not 1 <= max_objects <= 1_000_000:
            raise ContractError("Bounded object count required")
        invariants = dict(invariants or {})
        if not all(isinstance(r, ObjectReference) for r in invariants.values()):
            raise ContractError("Invariants are ObjectReference checks")
        self.sides, self.writers, self.app = sides, writers, app
        self.sets, self.invariants, self.outsider = dict(sets), invariants, outsider
        self.zone_of, self.max_objects = zone_of, max_objects

    # -- attempts --------------------------------------------------------------
    def _read(self, client, bucket, key):
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except (ClientError, BotoCoreError, OSError) as error:
            return _refusal(error)
        body = response.get("Body")
        if body is not None:  # authorization is the question: never read the bytes
            body.close()
        return "allowed"

    def _list(self, client, bucket, prefix=""):
        try:
            client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        except (ClientError, BotoCoreError, OSError) as error:
            return _refusal(error)
        return "allowed"

    def _write(self, client, bucket, admin, prefix=""):
        """A conditional write under ``prefix`` that cannot succeed. An allowed
        writer gets "no such key" or a failed precondition, a denied one an
        access denial."""
        key = prefix + PROBE_PREFIX.rstrip("/") + "-" + uuid4().hex
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"", IfMatch=NEVER_ETAG)
        except ClientError as error:
            status, code = _status(error)
            if code in ("NoSuchKey", "PreconditionFailed") or status in (404, 412):
                return "allowed"
            return _refusal(error)
        except (BotoCoreError, OSError) as error:
            return _refusal(error)
        admin.delete_object(Bucket=bucket, Key=key)  # the store ignored If-Match
        return "allowed:condition_ignored"

    def _writes(self, client, bucket, admin):
        """A writer is refused only if refused under every protected prefix: a
        denial of some prefixes leaves the others writable."""
        found = [self._write(client, bucket, admin, prefix) for prefix in self.sets.values()]
        return next((f for f in found if f != "refused"), "refused")

    def _tries(self, client, bucket, witnesses, admin):
        """Per protected set: read its witness, list its prefix, write under it."""
        return {name: {"read": self._read(client, bucket, key),
                       "list": self._list(client, bucket, self.sets[name]),
                       "write": self._write(client, bucket, admin, self.sets[name])}
                for name, key in witnesses.items()}

    @staticmethod
    def _flat(tries):
        return [outcome for per_set in tries.values() for outcome in per_set.values()]

    @staticmethod
    def _verdict(positives, attempts):
        """refused (pass), allowed (a decided failure) or undecided."""
        if any(a.startswith("allowed") for a in attempts):
            return "allowed"
        if positives and all(p == "allowed" for p in positives) and attempts and all(
                a == "refused" for a in attempts):
            return "refused"
        return "undecided"

    # -- write authority ---------------------------------------------------------
    def _side(self, name):
        if name not in self.sides:
            raise ContractError("Side is source or target")
        return self.sides[name]

    def _writer_attempts(self, side, names):
        s = self._side(side)
        return {n: self._writes(s.client_as(n), s.bucket, s.client) for n in names}

    def write_denied(self, side):
        attempts = self._writer_attempts(side, self.writers)
        view = self._side(side).access.denied(self.writers)
        refused = all(a == "refused" for a in attempts.values())
        return refused and all(view.values()), {
            "probe": "write_denied", "side": side, "attempt": attempts, "access_view": view,
            "views_agree": refused == all(view.values())}

    def single_writer(self):
        target, others = self._side("target"), tuple(w for w in self.writers if w != self.app)
        app_attempt = self._writer_attempts("target", (self.app,))[self.app]
        app_view = target.access.writable_by(self.app)
        other_attempts = self._writer_attempts("target", others)
        other_view = target.access.denied(others) if others else {}
        source_denied, source = self.write_denied("source")
        passed = (app_attempt == "allowed" and app_view
                  and all(a == "refused" for a in other_attempts.values())
                  and all(other_view.values()) and source_denied)
        return passed, {"probe": "single_writer",
                        "target_application": {"attempt": app_attempt, "access_view": app_view},
                        "target_others": {"attempt": other_attempts, "access_view": other_view},
                        "source": {k: source[k] for k in ("attempt", "access_view")}}

    # -- content -----------------------------------------------------------------
    def _keys(self, side, prefix, bucket=None):
        s, keys = self._side(side), []
        pages = s.client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket or s.bucket, Prefix=prefix)
        for page in pages:
            keys += [o["Key"] for o in page.get("Contents", ())]
            if len(keys) > self.max_objects:
                raise ContractError("More objects than the probe is bounded to read")
        return sorted(keys)

    @staticmethod
    def _fingerprint(client, bucket, key):
        response = client.get_object(Bucket=bucket, Key=key)
        h, size = hashlib.sha256(), 0
        for chunk in response["Body"].iter_chunks(_CHUNK):
            h.update(chunk)
            size += len(chunk)
        tags = client.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", ())
        return {"sha256": h.hexdigest(), "size": size,
                **{name: response.get(name) for name in _HEADERS},
                "metadata": dict(sorted((response.get("Metadata") or {}).items())),
                "tags": sorted([t["Key"], t["Value"]] for t in tags)}

    def _inventory(self, side, prefix, bucket=None):
        s = self._side(side)
        bucket = bucket or s.bucket
        return {key: self._fingerprint(s.client, bucket, key)
                for key in self._keys(side, prefix, bucket)}

    @staticmethod
    def _digest(inventory):
        text = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def content_equal(self, name):
        prefix = self.sets[name]
        source, target = self._inventory("source", prefix), self._inventory("target", prefix)
        differing = sorted(k for k in set(source) | set(target) if source.get(k) != target.get(k))
        return bool(source) and not differing, {
            "probe": "content_equal", "set": name, "prefix": prefix,
            "objects": [len(source), len(target)],
            "sha256": [self._digest(source), self._digest(target)],
            "differing": len(differing), "first_differing": differing[:20]}

    def invariant(self, name):
        reference = self.invariants.get(name)
        if reference is None:
            return False, {"probe": "invariant", "invariant": name, "reason": "not registered"}
        s = self._side("target")
        seen, missing, skipped = {}, [], 0
        for key in self._keys("target", reference.prefix):
            body = s.client.get_object(Bucket=s.bucket, Key=key)["Body"].read()
            try:
                document = json.loads(body)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(document, dict) or not all(
                    f in document for f in reference.fields):
                skipped += 1  # not a record the reference applies to
                continue
            values = tuple(str(document[f]) for f in reference.fields)
            seen[values] = seen.get(values, 0) + 1
            named = reference.template.format(**dict(zip(reference.fields, values)))
            try:
                s.client.head_object(Bucket=s.bucket, Key=named)
            except ClientError as error:
                if _status(error)[0] != 404:
                    raise
                missing.append(key)
        duplicates = sorted(v for v, n in seen.items() if n > 1)
        return bool(seen) and not missing and not duplicates, {
            "probe": "invariant", "invariant": name, "checked": len(seen),
            "skipped": skipped, "missing": len(missing), "first_missing": missing[:20],
            "duplicates": len(duplicates)}

    def restore_drill(self, scratch=None):
        """Restore the target's protected sets into a new scratch bucket of
        the target store, compare them there, and remove the bucket."""
        s = self._side("target")
        bucket = scratch if type(scratch) is str and scratch else (
            f"{s.bucket[:40].rstrip('-.')}-restore-{uuid4().hex[:10]}")
        try:
            s.client.create_bucket(Bucket=bucket)
        except ClientError as error:
            return False, {"probe": "restore_drill", "reason": "scratch bucket not created",
                           "code": _status(error)[1]}
        try:
            for prefix in self.sets.values():
                for key in self._keys("target", prefix):
                    response = s.client.get_object(Bucket=s.bucket, Key=key)
                    extra = {name: response[name] for name in _HEADERS if response.get(name)}
                    tags = s.client.get_object_tagging(Bucket=s.bucket, Key=key).get(
                        "TagSet", ())
                    if tags:
                        extra["Tagging"] = urlencode([(t["Key"], t["Value"]) for t in tags])
                    s.client.put_object(Bucket=bucket, Key=key, Body=response["Body"].read(),
                                        Metadata=response.get("Metadata") or {}, **extra)
            compared = {}
            for name, prefix in self.sets.items():
                original = self._inventory("target", prefix)
                restored = self._inventory("target", prefix, bucket=bucket)
                compared[name] = {"objects": len(original), "equal": original == restored,
                                  "sha256": self._digest(restored)}
            passed = all(c["equal"] and c["objects"] for c in compared.values())
            return passed, {"probe": "restore_drill", "sets": compared}
        finally:
            self._remove_bucket(bucket)

    def _remove_bucket(self, bucket):
        client = self._side("target").client
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for page in pages:
            for item in page.get("Contents", ()):
                client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)

    # -- access by others ----------------------------------------------------------
    def _witnesses(self, side):
        """Per protected set, one current object under its prefix to read: an
        existing one, or - before the data is there - a canary the probe's
        own client places under the prefix and removes afterwards. Access can
        differ by prefix, so every set is tried. Returns (witnesses, placed)."""
        s, witnesses, placed = self._side(side), {}, []
        for name, prefix in self.sets.items():
            found = s.client.list_objects_v2(Bucket=s.bucket, Prefix=prefix, MaxKeys=1)
            keys = [item["Key"] for item in found.get("Contents", ())]
            if keys:
                witnesses[name] = keys[0]
                continue
            key = prefix + "sova-probe-canary-" + uuid4().hex
            try:
                s.client.put_object(Bucket=s.bucket, Key=key, Body=uuid4().bytes)
            except (ClientError, BotoCoreError, OSError):
                continue  # no witness for this set: the probe stays undecided
            witnesses[name] = key
            placed.append(key)
        return witnesses, placed

    def _remove(self, side, keys):
        s = self._side(side)
        for key in keys:
            s.client.delete_object(Bucket=s.bucket, Key=key)

    def _positives(self, side, witnesses):
        s = self._side(side)
        return [self._read(s.client, s.bucket, key) for key in witnesses.values()]

    def anonymous_denied(self, side="target"):
        """No unsigned request reads, lists or writes any protected set. The
        positive control is the probe's own reader: the witness is there and
        the store answers (the application may itself be fenced off the
        target until activation)."""
        s = self._side(side)
        if s.anonymous is None:
            return False, {"probe": "anonymous_denied", "side": side,
                           "reason": "no unsigned client registered"}
        witnesses, placed = self._witnesses(side)
        try:
            before = self._positives(side, witnesses)
            tries = self._tries(s.anonymous, s.bucket, witnesses, s.client)
            after = self._positives(side, witnesses)
        finally:
            self._remove(side, placed)
        complete = len(witnesses) == len(self.sets)
        verdict = self._verdict(before + after if complete else [], self._flat(tries))
        return verdict == "refused", {
            "probe": "anonymous_denied", "side": side, "outcome": verdict,
            "canaries": len(placed), "positive": {"before": before, "after": after},
            "anonymous": tries}

    def cross_tenant_denied(self, other=None):
        """Another tenant cannot read, list or write any protected set of the
        application's target bucket, and the application cannot reach the
        other tenant's bucket (``other`` overrides the registered tenant).
        Positive controls: the probe's reader reads each witness, the other
        tenant its own object. A refusal counts only as an access denial, so
        a credential the store does not recognise is never a refusal."""
        other = other or self.outsider
        if other is None:
            return False, {"probe": "cross_tenant_denied", "reason": "no other tenant registered"}
        s = self._side("target")
        app = s.client_as(self.app)
        witnesses, placed = self._witnesses("target")
        try:
            before = self._positives("target", witnesses) + [
                self._read(other.client, other.bucket, other.key)]
            into_app = self._tries(other.client, s.bucket, witnesses, s.client)
            into_other = {"read": self._read(app, other.bucket, other.key),
                          "list": self._list(app, other.bucket),
                          "write": self._write(app, other.bucket, s.client)}
            after = self._positives("target", witnesses) + [
                self._read(other.client, other.bucket, other.key)]
        finally:
            self._remove("target", placed)
        complete = len(witnesses) == len(self.sets)
        verdict = self._verdict(before + after if complete else [],
                                [*self._flat(into_app), *into_other.values()])
        return verdict == "refused", {
            "probe": "cross_tenant_denied", "outcome": verdict, "canaries": len(placed),
            "positive": {"before": before, "after": after},
            "other_into_application": into_app, "application_into_other": into_other}

    # -- transport and placement ----------------------------------------------------
    def transport_encrypted(self, side):
        s = self._side(side)
        url = urlsplit(s.client.meta.endpoint_url)
        if s.tls is None:
            return False, {"probe": "transport_encrypted", "side": side, "scheme": url.scheme,
                           "reason": "no TLS identity registered"}
        same = (url.scheme == "https" and url.hostname == s.tls.server_name
                and (url.port or 443) == s.tls.port)
        shake = handshake(s.tls)
        return same and shake["outcome"] == "verified", {
            "probe": "transport_encrypted", "side": side, "scheme": url.scheme,
            "endpoint_registered": same, "handshake": shake}

    def transport_refusals(self, side):
        s = self._side(side)
        if s.tls is None:
            return False, {"probe": "transport_refusals", "side": side,
                           "reason": "no TLS identity registered"}
        passed, observation = transport_refusals(s.tls, plaintext=http_plaintext)
        return passed, {**observation, "side": side}

    def zone(self, side, allowed):
        label = self.zone_of(side) if self.zone_of else None
        return label in allowed, {"probe": "zone", "side": side, "label": label,
                                  "allowed": sorted(allowed),
                                  "source": "inventory" if self.zone_of else "none"}
