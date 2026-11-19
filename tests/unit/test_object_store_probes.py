"""Object-storage probes against an in-memory S3 store with real access decisions.

The store answers the way S3-compatible stores do (access denial 403,
``If-Match`` on a missing key 404); grants are per principal and bucket, so
the cases a real store produces can be set up exactly: an open store, a
denial kept in policy but not enforced, a store that ignores ``If-Match``.
"""

import io
from types import SimpleNamespace

from botocore.exceptions import ClientError
import pytest

from sova.adapters.object_store_probes import (
    ObjectReference,
    ObjectStoreProbes,
    ObjectStoreSide,
    ObjectTenant,
)
from sova.domain.contracts import ContractError

ANONYMOUS = None
OPS = ("read", "list", "write")


def _error(status, code, operation):
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class Body(io.BytesIO):
    def iter_chunks(self, size):
        while chunk := self.read(size):
            yield chunk


class Store:
    """Buckets of objects; ``grants[principal][bucket]`` the operations allowed,
    ``prefix_grants[principal]`` (bucket, prefix, operation) triples. The
    administrator may do anything; ``None`` is the anonymous caller."""

    def __init__(self, name="store"):
        self.name, self.buckets, self.grants, self.prefix_grants = name, {}, {}, {}
        self.ignore_if_match, self.fail = False, None

    def grant(self, principal, bucket, *ops):
        self.grants.setdefault(principal, {}).setdefault(bucket, set()).update(ops or OPS)

    def revoke(self, principal, bucket, *ops):
        self.grants.get(principal, {}).get(bucket, set()).difference_update(ops or OPS)


class Client:
    def __init__(self, store, principal="admin", endpoint="https://localhost:9443"):
        self.store, self.principal = store, principal
        self.meta = SimpleNamespace(endpoint_url=endpoint)

    def _check(self, bucket, op, operation, key=""):
        if self.store.fail and self.store.fail[0] == operation:
            raise _error(*self.store.fail[1:], operation)
        if bucket not in self.store.buckets:
            raise _error(404, "NoSuchBucket", operation)
        if self.principal == "admin" or op in self.store.grants.get(
                self.principal, {}).get(bucket, ()):
            return
        if any(b == bucket and key.startswith(p) and o == op and p
               for b, p, o in self.store.prefix_grants.get(self.principal, ())):
            return
        raise _error(403, "AccessDenied", operation)

    def create_bucket(self, Bucket):  # noqa: N803 - the S3 client's names
        if self.principal != "admin":
            raise _error(403, "AccessDenied", "CreateBucket")
        if Bucket in self.store.buckets:
            raise _error(409, "BucketAlreadyOwnedByYou", "CreateBucket")
        self.store.buckets[Bucket] = {}

    def delete_bucket(self, Bucket):  # noqa: N803
        assert not self.store.buckets[Bucket], "bucket not empty"
        del self.store.buckets[Bucket]

    def put_object(self, Bucket, Key, Body=b"", IfMatch=None, Metadata=None, Tagging=None,  # noqa: N803
                   **headers):
        self._check(Bucket, "write", "PutObject", Key)
        objects = self.store.buckets[Bucket]
        if IfMatch is not None and not self.store.ignore_if_match:
            if Key not in objects:
                raise _error(404, "NoSuchKey", "PutObject")
            raise _error(412, "PreconditionFailed", "PutObject")
        data = Body if isinstance(Body, bytes) else Body.read()
        tags = [dict(zip(("Key", "Value"), pair.split("=", 1)))
                for pair in (Tagging or "").split("&") if pair]
        objects[Key] = {"data": data, "Metadata": dict(Metadata or {}), "tags": tags,
                        **headers}

    def get_object(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, "read", "GetObject", Key)
        found = self.store.buckets[Bucket].get(Key)
        if found is None:
            raise _error(404, "NoSuchKey", "GetObject")
        return {"Body": Body(found["data"]), "Metadata": found["Metadata"],
                **{k: v for k, v in found.items() if k[0].isupper() and k != "Metadata"}}

    def head_object(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, "read", "HeadObject")
        if Key not in self.store.buckets[Bucket]:
            raise _error(404, "404", "HeadObject")
        return {}

    def get_object_tagging(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, "read", "GetObjectTagging")
        return {"TagSet": list(self.store.buckets[Bucket][Key]["tags"])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._check(Bucket, "write", "DeleteObject")
        self.store.buckets[Bucket].pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000):  # noqa: N803
        self._check(Bucket, "list", "ListObjectsV2", Prefix)
        keys = sorted(k for k in self.store.buckets[Bucket] if k.startswith(Prefix))[:MaxKeys]
        return {"Contents": [{"Key": k} for k in keys]}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return SimpleNamespace(paginate=lambda **kw: [self.list_objects_v2(**kw)])


class Access:
    """The access model's view, set independently of what the store enforces."""

    def __init__(self, denied=(), writable=()):
        self.denied_set, self.writable_set = set(denied), set(writable)

    def denied(self, principals, *, by=None):
        return {p: p in self.denied_set for p in principals}

    def writable_by(self, principal):
        return principal in self.writable_set


class World:
    def __init__(self, *, open_target=False):
        self.source_store, self.target_store = Store("source"), Store("target")
        self.buckets = {"source": "orders-src", "target": "orders-tgt"}
        for side, store in (("source", self.source_store), ("target", self.target_store)):
            Client(store).create_bucket(Bucket=self.buckets[side])
        self.target_store.buckets["other-tenant"] = {}
        Client(self.target_store).put_object(Bucket="other-tenant", Key="x/1.json", Body=b"1")
        self.target_store.grant("outsider", "other-tenant")
        for store, side in ((self.source_store, "source"), (self.target_store, "target")):
            for principal in ("app", "batch"):
                store.grant(principal, self.buckets[side])
        if open_target:
            self.target_store.grant(ANONYMOUS, self.buckets["target"])
        self.access = {"source": Access(), "target": Access()}
        for side in ("source", "target"):
            client = Client(self.store(side))
            for i in range(3):
                client.put_object(Bucket=self.buckets[side], Key=f"orders/{i}.json",
                                  Body=f'{{"order": {i}, "customer": "c", "key": "k{i}"}}'.encode(),
                                  ContentType="application/json", Metadata={"c": "1"},
                                  Tagging="class=invoice" if i == 1 else None)
                client.put_object(Bucket=self.buckets[side], Key=f"results/c/k{i}.json",
                                  Body=b'{"result": "ok"}')

    def store(self, side):
        return self.source_store if side == "source" else self.target_store

    def side(self, name, tls=None):
        store = self.store(name)
        return ObjectStoreSide(client=Client(store), bucket=self.buckets[name],
                               access=self.access[name],
                               client_as=lambda principal: Client(store, principal),
                               anonymous=Client(store, ANONYMOUS), tls=tls)

    def probes(self, **kwargs):
        return ObjectStoreProbes(
            sides={"source": self.side("source"), "target": self.side("target")},
            writers=("app", "batch"), app="app",
            sets={"orders": "orders/", "results": "results/"},
            invariants={"ORDER-01": ObjectReference("orders/", "results/{customer}/{key}.json",
                                                    ("customer", "key"))},
            outsider=ObjectTenant(Client(self.target_store, "outsider"), "other-tenant",
                                  "x/1.json"), **kwargs)

    def fence(self, side):
        for principal in ("app", "batch"):
            self.store(side).revoke(principal, self.buckets[side], "write")
        self.access[side].denied_set |= {"app", "batch"}


def test_a_denial_counts_only_when_the_attempt_and_the_access_view_agree():
    world = World()
    passed, observation = world.probes().write_denied("source")
    assert not passed and observation["attempt"] == {"app": "allowed", "batch": "allowed"}
    world.fence("source")
    passed, observation = world.probes().write_denied("source")
    assert passed and observation["views_agree"]
    assert not any("sova-probe" in k for k in world.source_store.buckets["orders-src"])


def test_a_denial_kept_in_policy_but_not_enforced_is_caught():
    """The policy says denied (as SeaweedFS returns a policy naming a principal
    it does not resolve); the store still takes the writer's writes."""
    world = World()
    world.access["source"].denied_set |= {"app", "batch"}
    passed, observation = world.probes().write_denied("source")
    assert not passed
    assert observation["access_view"] == {"app": True, "batch": True}
    assert observation["attempt"]["app"] == "allowed" and not observation["views_agree"]


def test_a_refusal_the_access_model_does_not_explain_does_not_pass():
    world = World()
    for principal in ("app", "batch"):
        world.source_store.revoke(principal, "orders-src", "write")
    passed, observation = world.probes().write_denied("source")
    assert not passed and not observation["views_agree"]


def test_a_store_that_ignores_if_match_is_an_allowed_write_and_leaves_nothing():
    world = World()
    world.source_store.ignore_if_match = True
    passed, observation = world.probes().write_denied("source")
    assert not passed and observation["attempt"]["app"] == "allowed:condition_ignored"
    assert not any("sova-probe" in k for k in world.source_store.buckets["orders-src"])


def test_only_the_application_writes_the_target_after_activation():
    world = World()
    world.fence("source")
    world.target_store.revoke("batch", "orders-tgt", "write")
    world.access["target"] = Access(denied={"batch"}, writable={"app"})
    passed, observation = world.probes().single_writer()
    assert passed, observation
    world.target_store.grant("batch", "orders-tgt", "write")
    passed, observation = world.probes().single_writer()
    assert not passed and observation["target_others"]["attempt"] == {"batch": "allowed"}


def test_sets_compare_bytes_headers_metadata_and_tags():
    world = World()
    assert world.probes().content_equal("orders")[0]
    world.target_store.buckets["orders-tgt"]["orders/1.json"]["tags"] = []
    passed, observation = world.probes().content_equal("orders")
    assert not passed and observation["first_differing"] == ["orders/1.json"]
    assert "invoice" not in str(observation)  # digests, never contents


def test_an_empty_set_on_the_source_does_not_pass():
    world = World()
    probes = ObjectStoreProbes(sides={"source": world.side("source"),
                                      "target": world.side("target")},
                               writers=("app",), app="app", sets={"typo": "ordrs/"})
    passed, observation = probes.content_equal("typo")
    assert not passed and observation["objects"] == [0, 0]


def test_the_reference_invariant_finds_missing_and_repeated_records():
    world = World()
    assert world.probes().invariant("ORDER-01")[0]
    Client(world.target_store).put_object(Bucket="orders-tgt", Key="orders/9.json",
                                          Body=b'{"order": 9}')  # no key yet: skipped
    passed, observation = world.probes().invariant("ORDER-01")
    assert passed and observation["skipped"] == 1
    Client(world.target_store).delete_object(Bucket="orders-tgt", Key="results/c/k2.json")
    Client(world.target_store).put_object(Bucket="orders-tgt", Key="orders/7.json",
                                          Body=b'{"customer": "c", "key": "k0"}')
    passed, observation = world.probes().invariant("ORDER-01")
    assert not passed and observation["missing"] == 1 and observation["duplicates"] == 1
    assert not world.probes().invariant("UNKNOWN")[0]


def test_the_restore_drill_compares_in_a_scratch_bucket_and_removes_it():
    world = World()
    passed, observation = world.probes().restore_drill()
    assert passed, observation
    assert set(world.target_store.buckets) == {"orders-tgt", "other-tenant"}
    world.target_store.fail = ("PutObject", 500, "InternalError")
    with pytest.raises(ClientError):
        world.probes().restore_drill()
    assert set(world.target_store.buckets) == {"orders-tgt", "other-tenant"}


REFUSED = {"read": "refused", "list": "refused", "write": "refused"}


def test_anonymous_access_is_refused_between_two_positive_controls():
    world = World()
    passed, observation = world.probes().anonymous_denied()
    assert passed and observation["anonymous"] == {"orders": REFUSED, "results": REFUSED}
    assert observation["positive"] == {"before": ["allowed"] * 2, "after": ["allowed"] * 2}
    assert observation["canaries"] == 0


def test_an_open_target_fails_anonymous_access_decidedly():
    world = World(open_target=True)
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["outcome"] == "allowed"
    assert observation["anonymous"]["orders"]["read"] == "allowed"


def test_one_open_operation_is_enough_to_fail():
    world = World()
    world.target_store.grant(ANONYMOUS, "orders-tgt", "list")
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["outcome"] == "allowed"
    assert observation["anonymous"]["orders"] == {"read": "refused", "list": "allowed",
                                                  "write": "refused"}


def test_one_open_prefix_is_enough_to_fail():
    """Only ``results/`` is public: a single registered key under ``orders/``
    would have passed."""
    world = World()
    world.target_store.prefix_grants[ANONYMOUS] = [("orders-tgt", "results/", "read")]
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["outcome"] == "allowed"
    assert observation["anonymous"]["orders"] == REFUSED
    assert observation["anonymous"]["results"]["read"] == "allowed"


def test_before_the_copy_canaries_stand_in_for_the_sets_and_are_removed():
    world = World()
    world.target_store.buckets["orders-tgt"].clear()
    passed, observation = world.probes().anonymous_denied()
    assert passed and observation["canaries"] == 2
    assert world.target_store.buckets["orders-tgt"] == {}
    world.target_store.prefix_grants[ANONYMOUS] = [("orders-tgt", "orders/", "read")]
    passed, observation = world.probes().cross_tenant_denied()
    assert passed and observation["canaries"] == 2
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["anonymous"]["orders"]["read"] == "allowed"
    assert world.target_store.buckets["orders-tgt"] == {}


def test_a_refusal_of_a_missing_object_is_undecided():
    """The witness is gone when read: the store refuses the anonymous caller
    as it would refuse any read of a missing key, which proves nothing."""
    world = World()
    probes = world.probes()
    probes._witnesses = lambda side: ({"orders": "orders/gone.json",
                                       "results": "results/c/k0.json"}, [])
    passed, observation = probes.anonymous_denied()
    assert not passed and observation["outcome"] == "undecided"
    assert observation["positive"]["before"][0] == "undecided:NoSuchKey"


def test_a_set_without_a_witness_is_undecided():
    world = World()
    world.target_store.buckets["orders-tgt"].clear()
    world.target_store.fail = ("PutObject", 500, "InternalError")  # no canary can be placed
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["outcome"] == "undecided"


def test_errors_that_are_not_access_denials_are_undecided():
    world = World()
    probes = world.probes()
    probes._witnesses = lambda side: ({"orders": "orders/0.json",
                                       "results": "results/c/k0.json"}, [])
    world.target_store.fail = ("ListObjectsV2", 500, "InternalError")
    passed, observation = probes.anonymous_denied()
    assert not passed and observation["outcome"] == "undecided"
    assert observation["anonymous"]["orders"]["list"] == "undecided:InternalError"


def test_another_tenant_is_refused_both_ways():
    world = World()
    passed, observation = world.probes().cross_tenant_denied()
    assert passed, observation
    assert observation["positive"] == {"before": ["allowed"] * 3, "after": ["allowed"] * 3}
    assert observation["other_into_application"] == {"orders": REFUSED, "results": REFUSED}
    assert observation["application_into_other"] == REFUSED


@pytest.mark.parametrize("who, bucket", [("outsider", "orders-tgt"), ("app", "other-tenant")])
def test_isolation_fails_when_either_direction_is_open(who, bucket):
    world = World()
    world.target_store.grant(who, bucket, "read")
    passed, observation = world.probes().cross_tenant_denied()
    assert not passed and observation["outcome"] == "allowed"


def test_isolation_without_the_other_tenants_positive_control_is_undecided():
    world = World()
    world.target_store.revoke("outsider", "other-tenant", "read")
    passed, observation = world.probes().cross_tenant_denied()
    assert not passed and observation["outcome"] == "undecided"


def test_a_credential_the_store_does_not_know_is_never_a_refusal():
    world = World()
    probes = world.probes()
    unknown = Client(world.target_store, "outsider")

    def invalid(**kwargs):
        raise _error(403, "InvalidAccessKeyId", "GetObject")
    unknown.get_object = unknown.list_objects_v2 = unknown.put_object = invalid
    passed, observation = probes.cross_tenant_denied(
        ObjectTenant(unknown, "other-tenant", "x/1.json"))
    assert not passed and observation["outcome"] == "undecided"


def test_without_a_registered_tenant_or_unsigned_client_nothing_passes():
    world = World()
    probes = ObjectStoreProbes(
        sides={"source": world.side("source"),
               "target": ObjectStoreSide(Client(world.target_store), "orders-tgt",
                                         Access(), lambda p: Client(world.target_store, p))},
        writers=("app",), app="app", sets={"orders": "orders/"})
    assert not probes.cross_tenant_denied()[0]
    assert not probes.anonymous_denied()[0]
    assert not probes.transport_encrypted("target")[0]
    assert not probes.transport_refusals("target")[0]


def test_construction_refuses_an_incomplete_registration():
    world = World()
    sides = {"source": world.side("source"), "target": world.side("target")}
    with pytest.raises(ContractError):
        ObjectStoreProbes(sides=sides, writers=("batch",), app="app", sets={"o": "o/"})
    with pytest.raises(ContractError):
        ObjectStoreProbes(sides={"target": sides["target"]}, writers=("app",), app="app",
                          sets={"o": "o/"})
    with pytest.raises(ContractError):
        ObjectReference("orders/", "results/{customer}.json", ("customer", "key"))


def test_an_anonymous_write_a_store_took_despite_if_match_is_a_decided_failure():
    world = World()
    world.target_store.grant(ANONYMOUS, "orders-tgt", "write")
    world.target_store.ignore_if_match = True
    passed, observation = world.probes().anonymous_denied()
    assert not passed and observation["outcome"] == "allowed"
    assert observation["anonymous"]["orders"]["write"] == "allowed:condition_ignored"
    assert not any("sova-probe" in k for k in world.target_store.buckets["orders-tgt"])


def test_a_writer_open_under_one_prefix_is_not_denied():
    world = World()
    world.fence("source")
    world.source_store.prefix_grants["batch"] = [("orders-src", "results/", "write")]
    passed, observation = world.probes().write_denied("source")
    assert not passed and observation["attempt"] == {"app": "refused", "batch": "allowed"}


def test_a_set_left_without_a_witness_keeps_the_probe_undecided():
    world = World()
    probes = world.probes()
    probes._witnesses = lambda side: ({"orders": "orders/0.json"}, [])  # results: none
    passed, observation = probes.anonymous_denied()
    assert not passed and observation["outcome"] == "undecided"
    assert not probes.cross_tenant_denied()[0]
