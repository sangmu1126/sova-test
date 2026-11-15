"""GCS as an object-storage side, on recorded REST answers."""

import io
import json

import pytest

from sova.adapters.gcp_rest import GcpRest
from sova.adapters.gcs_store import GcsBucketIamAccess, GcsS3Client
from sova.domain.contracts import ContractError

B = "https://storage.googleapis.com/storage/v1/b/orders-src"
TROUBLE = "https://policytroubleshooter.googleapis.com/v3/iam:troubleshoot"
APP, OTHER = "serviceAccount:app@p.iam.gserviceaccount.com", "serviceAccount:batch@p.iam.gserviceaccount.com"


class Raw:
    def __init__(self, data):
        self.data, self.decoded = io.BytesIO(data), []

    def read(self, size=-1, decode_content=True):
        self.decoded.append(decode_content)
        return self.data.read(size)


class Response:
    def __init__(self, status, body=None, raw=b"", headers=None):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.raw, self.headers = Raw(raw), headers or {}

    def json(self):
        return self._body

    def close(self):
        pass


class Store:
    def __init__(self):
        self.policy = {"etag": "e1", "version": 3, "bindings": [
            {"role": "roles/storage.objectAdmin", "members": [APP, OTHER]},
            {"role": "roles/storage.objectViewer", "members": [OTHER, "group:auditors@x.test"]}]}
        self.labels = {"sova-test": "run1"}
        self.metageneration = 5  # GCS: IAM and label writes both bump it
        self.access = {}
        self.requests = []
        self.chunks = []

    def request(self, method, url, json=None, params=None, timeout=None, data=None,
                headers=None, stream=False):
        self.requests.append((method, url, params, headers))
        if url == B + "/iam" and method == "GET":
            return Response(200, self.policy)
        if url == B + "/iam" and method == "PUT":
            if json.get("etag") != self.policy["etag"]:
                return Response(412, {"error": {"status": "FAILED_PRECONDITION"}})
            self.policy = {**json, "etag": "e2"}
            self.metageneration += 1
            return Response(200, self.policy)
        if url == B and method == "GET":
            return Response(200, {"name": "orders-src", "labels": dict(self.labels),
                                  "metageneration": str(self.metageneration)})
        if url == B and method == "PATCH":
            expected = (params or {}).get("ifMetagenerationMatch")
            if expected is not None and expected != str(self.metageneration):
                return Response(412, {"error": {"status": "FAILED_PRECONDITION"}})
            self.metageneration += 1
            for k, v in json["labels"].items():
                if v is None:
                    self.labels.pop(k, None)
                else:
                    self.labels[k] = v
            return Response(200, {})
        if url == TROUBLE:
            member = json["accessTuple"]["principal"]
            return Response(200, {"overallAccessState": self.access.get(member, "CAN_ACCESS")})
        if url == B + "/o/a%2Fb.gz" and not params:
            return Response(200, {"name": "a/b.gz", "generation": "77", "size": "3",
                                  "contentEncoding": "gzip", "metadata": {"k": "v"}})
        if url == B + "/o/a%2Fb.gz" and params.get("alt") == "media":
            return Response(200, raw=b"\x1f\x8b\x08")
        if url.endswith("/upload/storage/v1/b/orders-src/o"):
            return Response(200, headers={"Location": "https://upload/session"})
        if url == "https://upload/session":
            self.chunks.append((headers["Content-Range"], len(data)))
            last = not headers["Content-Range"].endswith("/*")
            return Response(200 if last else 308, {"etag": "x"} if last else None)
        return Response(404, {"error": {"status": "NOT_FOUND"}})


def rest(store):
    return GcpRest(store, "sova-test-project")


class Clock:
    def __init__(self):
        self.now, self.slept = 1_000_000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def access(store, clock=None, window=420):
    clock = clock or Clock()
    return GcsBucketIamAccess(rest(store), "orders-src", principals={"app": APP, "other": OTHER},
                              propagation_seconds=window, clock=clock, sleep=clock.sleep)


def test_deny_removes_only_those_members_and_marks_the_statement():
    store = Store()
    access(store).deny("FenceWrites", ("app", "other"))
    assert store.policy["bindings"] == [
        {"role": "roles/storage.objectViewer", "members": ["group:auditors@x.test"]}]
    assert store.labels["sova-handoff-fencewrites"] == "deny"
    assert store.labels["sova-test"] == "run1" and store.labels["sova-handoff-at"]


def test_a_policy_changed_in_between_is_not_overwritten():
    store = Store()
    a = access(store)
    read = store.request

    def racing(method, url, **kwargs):
        answer = read(method, url, **kwargs)
        if method == "GET" and url == B + "/iam":
            store.policy = {**store.policy, "etag": "e9"}  # someone else wrote meanwhile
        return answer

    store.request = racing
    with pytest.raises(ContractError, match="changed in between"):
        a.deny("FenceWrites", ("app",))


def test_only_cannot_access_is_denied_and_only_can_access_is_writable():
    store = Store()
    a = access(store)
    store.access = {"app@p.iam.gserviceaccount.com": "CANNOT_ACCESS",
                    "batch@p.iam.gserviceaccount.com": "UNKNOWN_CONDITIONAL"}
    assert a.denied(("app", "other")) == {"app": False, "other": False}  # nothing denied yet
    a.deny("PauseAdmission", ("other",))
    assert a.denied(("app", "other")) == {"app": True, "other": False}
    assert a.denied(("app",), by="FenceWrites") == {"app": False}  # not that marker
    a.deny("FenceWrites", ("app",))
    assert a.denied(("app",), by="FenceWrites") == {"app": True}
    assert a.writable_by("other") is False
    store.access = {}
    assert a.writable_by("app") is True
    with pytest.raises(ContractError):
        a.denied(("nobody",))


def test_objects_are_read_as_stored_at_the_generation_read():
    store = Store()
    found = GcsS3Client(rest(store)).get_object(Bucket="orders-src", Key="a/b.gz")
    assert found["ContentEncoding"] == "gzip" and found["Metadata"] == {"k": "v"}
    assert b"".join(found["Body"].iter_chunks(2)) == b"\x1f\x8b\x08"
    method, url, params, headers = store.requests[-1]
    assert params == {"alt": "media", "generation": "77"}
    assert headers == {"Accept-Encoding": "gzip"}  # no decompressive transcoding
    assert found["Body"].response.raw.decoded and not any(found["Body"].response.raw.decoded)


def test_uploads_are_streamed_in_resumable_chunks(monkeypatch):
    import sova.adapters.gcs_store as gcs

    monkeypatch.setattr(gcs, "_CHUNK", 4)
    store = Store()
    GcsS3Client(rest(store)).upload_fileobj(io.BytesIO(b"0123456789"), "orders-src", "k",
                                            ExtraArgs={"ContentType": "text/plain"})
    assert store.chunks == [("bytes 0-3/*", 4), ("bytes 4-7/*", 4), ("bytes 8-9/10", 2)]
    with pytest.raises(ContractError, match="no tags"):
        GcsS3Client(rest(store)).put_object(Bucket="orders-src", Key="k", Body=b"x",
                                            Tagging="a=b")


def test_a_denial_counts_only_after_the_propagation_window_and_while_unchanged():
    store, clock = Store(), Clock()
    a = access(store, clock, window=420)
    store.access = {"app@p.iam.gserviceaccount.com": "CANNOT_ACCESS",
                    "batch@p.iam.gserviceaccount.com": "CANNOT_ACCESS"}
    a.deny("PauseAdmission", ("app", "other"))
    assert clock.slept == [420]  # the fencing step itself waits the window out
    assert a.denied(("app", "other")) == {"app": True, "other": True}
    a.deny("FenceWrites", ("app", "other"))  # nothing left to remove: no new wait
    assert clock.slept == [420]
    store.policy["bindings"].append({"role": "roles/storage.objectViewer",
                                     "members": ["user:someone@x.test"]})  # an outside edit
    assert a.denied(("app",)) == {"app": False}  # its time is unknown
    a.deny("FenceWrites", ("app", "other"))  # adopting it starts the window again
    assert clock.slept == [420, 420] and a.denied(("app",)) == {"app": True}
    store.metageneration += 1  # any other change to the bucket, e.g. its labels
    assert a.denied(("app",)) == {"app": False}


def test_the_handoffs_own_label_write_does_not_unsettle_it():
    """On GCS the policy etag follows the bucket's metadata, so the label
    write itself would look like a change; the metageneration it records is
    the one its own conditional write produces."""
    store, clock = Store(), Clock()
    a = access(store, clock, window=10)
    store.access = {"app@p.iam.gserviceaccount.com": "CANNOT_ACCESS"}
    a.deny("PauseAdmission", ("app",))
    assert store.labels["sova-handoff-meta"] == str(store.metageneration)
    assert a.denied(("app",)) == {"app": True}


def test_a_fresh_grant_is_not_yet_writable():
    store, clock = Store(), Clock()
    a = access(store, clock, window=60)
    store.policy["bindings"] = []
    clock.sleep = lambda seconds: None  # observe before the window ends
    a.sleep = clock.sleep
    a.open_writer("app")
    assert a.writable_by("app") is False
    clock.now += 60
    assert a.writable_by("app") is True


def test_a_bucket_nobody_may_write_is_not_staged_until_the_handoff_denies():
    store = Store()
    store.policy["bindings"] = []
    store.access = {"app@p.iam.gserviceaccount.com": "CANNOT_ACCESS",
                    "batch@p.iam.gserviceaccount.com": "CANNOT_ACCESS"}
    a = access(store, window=0)
    assert a.denied(("app", "other")) == {"app": False, "other": False}
    a.deny("TargetNoAccess", ("app", "other"))
    assert a.denied(("app", "other")) == {"app": True, "other": True}
