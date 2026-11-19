"""The object-storage probes against real stores: SeaweedFS as it starts by
default, and the same server locked (identities, TLS).

The open store is the target an on-premises move lands on when nobody locks
it: every probe that asks "who else can reach this" must fail it decidedly.
The locked store must pass the same probes, between positive controls. One
property of SeaweedFS shows why a write denial needs the attempt as well as
the policy: a bucket policy naming an identity the store does not resolve is
stored and returned, and does not stop the writer.
"""

import json
from uuid import uuid4

import pytest
from seaweedfs_fixtures import principal

from sova.adapters.object_store_probes import (
    ObjectReference,
    ObjectStoreProbes,
    ObjectStoreSide,
    ObjectTenant,
)
from sova.adapters.s3_bucket_policy_access import S3BucketPolicyAccess
from sova.adapters.tls_refusal import TlsEndpoint

pytestmark = [pytest.mark.seaweedfs]
WRITERS = ("app", "other")
SETS = {"orders": "orders/", "results": "results/"}


class Unit:
    """Source and target buckets of one application on one store, with
    business written to both, and another tenant's bucket beside them."""

    def __init__(self, store, *, names=None):
        self.store, suffix = store, uuid4().hex[:10]
        self.admin = store["client"]("admin")
        self.buckets = {"source": f"orders-src-{suffix}", "target": f"orders-tgt-{suffix}"}
        self.names = names or {w: principal(w) for w in WRITERS}
        for side, bucket in self.buckets.items():
            self.admin.create_bucket(Bucket=bucket)
            app = store["client"]("app")
            for i in range(4):
                app.put_object(Bucket=bucket, Key=f"orders/{i:04d}.json",
                               Body=json.dumps({"customer": "c-1", "key": f"k{i}"}).encode(),
                               ContentType="application/json", Metadata={"customer": "c-1"},
                               **({"Tagging": "class=invoice"} if i == 2 else {}))
                app.put_object(Bucket=bucket, Key=f"results/c-1/k{i}.json", Body=b'{"ok": 1}')
        self.other_bucket = f"other-{suffix}"
        self.admin.create_bucket(Bucket=self.other_bucket)
        store["client"]("outsider").put_object(Bucket=self.other_bucket, Key="x/1.json",
                                               Body=b"1")

    def access(self, side):
        return S3BucketPolicyAccess(self.admin, self.buckets[side], principals=self.names)

    def side(self, name):
        tls = None
        if "ca_pem" in self.store:
            tls = TlsEndpoint("127.0.0.1", self.store["port"], "localhost", self.store["ca_pem"])
        return ObjectStoreSide(client=self.admin, bucket=self.buckets[name],
                               access=self.access(name), client_as=self.store["client"],
                               anonymous=self.store["client"](None), tls=tls)

    def probes(self):
        return ObjectStoreProbes(
            sides={s: self.side(s) for s in ("source", "target")}, writers=WRITERS,
            app="app", sets=SETS,
            invariants={"ORDER-01": ObjectReference("orders/", "results/{customer}/{key}.json",
                                                    ("customer", "key"))},
            outsider=ObjectTenant(self.store["client"]("outsider"), self.other_bucket,
                                  "x/1.json"))


def test_the_store_as_it_starts_by_default_serves_anyone(seaweedfs_stores):
    probes = Unit(seaweedfs_stores["open"]).probes()
    passed, observation = probes.anonymous_denied()
    assert not passed and observation["outcome"] == "allowed"
    assert observation["anonymous"] == {"read": "allowed", "list": "allowed",
                                        "write": "allowed"}
    passed, observation = probes.cross_tenant_denied()
    assert not passed and observation["outcome"] == "allowed"
    passed, observation = probes.transport_encrypted("target")
    assert not passed and observation["scheme"] == "http"


def test_the_locked_store_refuses_anonymous_callers_and_other_tenants(seaweedfs_stores):
    probes = Unit(seaweedfs_stores["locked"]).probes()
    passed, observation = probes.anonymous_denied()
    assert passed, observation
    assert observation["positive"] == ["allowed", "allowed"]
    passed, observation = probes.cross_tenant_denied()
    assert passed, observation
    assert observation["other_into_application"] == observation["application_into_other"] == {
        "read": "refused", "list": "refused", "write": "refused"}


def test_the_locked_store_keeps_its_tls_identity_and_refuses_plain_http(seaweedfs_stores):
    probes = Unit(seaweedfs_stores["locked"]).probes()
    passed, observation = probes.transport_encrypted("target")
    assert passed, observation
    passed, observation = probes.transport_refusals("target")
    assert passed, observation
    checks = observation["checks"]
    assert checks["wrong_ca"]["outcome"] == checks["wrong_host"]["outcome"] == "refused"
    assert checks["plaintext"]["reason"] == "https_only_answer"


def test_a_policy_naming_an_identity_the_store_does_not_resolve_is_caught(seaweedfs_stores):
    """The fence written with bare identity names reads back as a denial;
    the writers still write. With the names the store resolves, both views
    agree and the fence holds."""
    store = seaweedfs_stores["locked"]
    bare = Unit(store, names={w: w if w == "app" else "batch" for w in WRITERS})
    bare.access("source").deny("FenceWrites", WRITERS)
    passed, observation = bare.probes().write_denied("source")
    assert not passed
    assert observation["access_view"] == {"app": True, "other": True}
    assert observation["attempt"] == {"app": "allowed", "other": "allowed"}
    resolved = Unit(store)
    resolved.access("source").deny("FenceWrites", WRITERS)
    passed, observation = resolved.probes().write_denied("source")
    assert passed and observation["views_agree"], observation


def test_only_the_application_writes_the_target_after_activation(seaweedfs_stores):
    unit = Unit(seaweedfs_stores["locked"])
    unit.access("source").deny("FenceWrites", WRITERS)
    unit.access("target").deny("TargetNoAccess", WRITERS)
    unit.access("target").open_writer("app")
    passed, observation = unit.probes().single_writer()
    assert passed, observation


def test_content_invariant_and_restore_on_the_locked_store(seaweedfs_stores):
    unit = Unit(seaweedfs_stores["locked"])
    probes = unit.probes()
    for name in SETS:
        passed, observation = probes.content_equal(name)
        assert passed, observation
    assert probes.invariant("ORDER-01")[0]
    passed, observation = probes.restore_drill()
    assert passed, observation
    names = [b["Name"] for b in unit.admin.list_buckets()["Buckets"]]
    assert not [n for n in names if "-restore-" in n]
    unit.admin.put_object_tagging(Bucket=unit.buckets["target"], Key="orders/0002.json",
                                  Tagging={"TagSet": []})
    passed, observation = probes.content_equal("orders")
    assert not passed and observation["first_differing"] == ["orders/0002.json"]
