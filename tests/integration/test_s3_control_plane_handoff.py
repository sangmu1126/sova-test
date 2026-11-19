"""The same compiled handoff, with object storage as the data: the protocol does not change.

The plan, its approval, admission, the authority record, the effect ledger and
the signed evidence are exactly those of the PostgreSQL handoffs; only the
adapter (S3-compatible offline copy between two independent stores) and the
measurements behind each obligation differ. The unit's data sets become object
prefixes: ``data.orders`` compares everything under ``orders/``, ORDER-01 is
checked on the order and result objects, residency on the target bucket's
zone tag, the restore test by restoring the target into a fresh bucket - all
through the unit's ``ObjectStoreProbes``.

The target's controls include who else can reach the bucket: an anonymous
caller, another tenant, and - where the target serves TLS - a wrong CA, a
wrong host name and plain HTTP. Onto a SeaweedFS target left as it starts by
default they fail before writes pause; onto the same store locked, the unit
moves.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from handoff_drive import drive, local_budgets
from handoff_support import admitted_handoff
import pytest
from s3_world import S3World
from seaweedfs_fixtures import principal

from sova.adapters.object_store_probes import (
    ObjectReference,
    ObjectStoreProbes,
    ObjectStoreSide,
    ObjectTenant,
)
from sova.adapters.tls_refusal import TlsEndpoint
from sova.domain.handoff_authority import HandoffState

pytestmark = [pytest.mark.postgres, pytest.mark.s3]
ZONE = "approved-zone-a"
DATA_SETS = ("orders", "work_items", "idempotency_results")


class OrdersInObjects(S3World):
    """Orders, work items and idempotency results as objects; a zone-tagged target."""

    def __init__(self, stores, *, adapter_id):
        super().__init__(stores, adapter_id=adapter_id)
        app, bucket = stores["as"]("source", "app"), self.buckets["source"]
        for i in range(1, 11):  # the orders the base world wrote get their companions
            key = f"key-{i}"
            app.put_object(Bucket=bucket, Key=f"idempotency_results/c-{i % 3}/{key}.json",
                           Body=b'{"result": "confirmed"}', ContentType="application/json")
            app.put_object(Bucket=bucket, Key=f"work_items/{i:04d}.json",
                           Body=json.dumps({"order": i, "state": "done"}).encode(),
                           ContentType="application/json")
            app.put_object(Bucket=bucket, Key=f"orders/{i:04d}.json",
                           Body=json.dumps({"order": i, "customer": f"c-{i % 3}",
                                            "idempotency_key": key}).encode(),
                           ContentType="application/json", Metadata={"customer": f"c-{i % 3}"})
        target = self.root["target"]  # prepared by the environment owner, zone-labelled
        target.create_bucket(Bucket=self.buckets["target"])
        target.put_bucket_tagging(Bucket=self.buckets["target"],
                                  Tagging={"TagSet": [{"Key": "zone", "Value": ZONE}]})

    def prefix_digest(self, side, prefix):
        client, bucket = self.root[side], self.buckets[side]
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        h, count = hashlib.sha256(), 0
        for key in sorted(o["Key"] for p in pages for o in p.get("Contents", ())):
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            h.update(f"{key}:{len(body)}:".encode() + body)
            count += 1
        return {"objects": count, "sha256": h.hexdigest()}


def object_controls(*controls):
    """The unit's contract with exactly ``controls`` required of the target."""
    def adjust(data):
        data["spec"]["security"]["required_controls"] = list(controls)
        local_budgets(data)
    return adjust


LOCAL_CONTROLS = ("deny_anonymous_data_access", "deny_cross_tenant_data_access",
                  "tested_restore")  # the local gateways serve plain HTTP
ON_PREMISES_CONTROLS = LOCAL_CONTROLS + ("verified_transport_encryption",
                                         "refuse_unverified_transport")


def other_tenant(world, client_of, create):
    """Another tenant's bucket on the target store, with one object of its own."""
    bucket = "other-" + uuid4().hex[:10]
    create(bucket)
    client_of().put_object(Bucket=bucket, Key="x/1.json", Body=b"{}")
    return ObjectTenant(client_of(), bucket, "x/1.json")


def object_probes(world, *, outsider=None, anonymous=None, tls=None):
    def side(name):
        client, bucket, access = world.side(name)
        return ObjectStoreSide(client=client, bucket=bucket, access=access,
                               client_as=lambda principal: world.s["as"](name, principal),
                               anonymous=anonymous(name) if anonymous else None,
                               tls=(tls or {}).get(name))

    def zone_of(name):
        tags = world.root[name].get_bucket_tagging(Bucket=world.buckets[name])
        return {t["Key"]: t["Value"] for t in tags["TagSet"]}.get("zone")

    return ObjectStoreProbes(
        sides={"source": side("source"), "target": side("target")}, writers=("app", "other"),
        app="app", sets={name: name + "/" for name in DATA_SETS},
        invariants={"ORDER-01": ObjectReference(
            "orders/", "idempotency_results/{customer}/{idempotency_key}.json",
            ("customer", "idempotency_key"))},
        outsider=outsider, zone_of=zone_of)


def measurements(probes, plan_doc):
    """Every obligation of the plan, measured by the unit's probes."""
    restore = lambda _: probes.restore_drill()  # noqa: E731
    source_denied = lambda _: probes.write_denied("source")  # noqa: E731
    measures = {
        "business.ORDER-01": lambda _: probes.invariant("ORDER-01"),
        "fence.source_writes_denied": source_denied,
        "route.stale_writes_rejected": source_denied,
        "authority.single_writer_after_activation": lambda _: probes.single_writer(),
        "security.deny_anonymous_data_access": lambda _: probes.anonymous_denied("target"),
        "security.deny_cross_tenant_data_access": lambda _: probes.cross_tenant_denied(),
        "security.verified_transport_encryption": lambda _: probes.transport_encrypted("target"),
        "security.refuse_unverified_transport": lambda _: probes.transport_refusals("target"),
        "security.tested_restore": restore, "operations.restore_test": restore}
    for o in plan_doc["obligations"]:
        if o["kind"] == "placement_evidence":
            measures[o["id"]] = lambda ob: probes.zone("target", set(ob["allowed_zones"]))
        elif o["kind"] == "final_content_comparison":
            name = o["id"].split(".", 1)[1]
            measures[o["id"]] = lambda _, name=name: probes.content_equal(name)
    return measures


def local_unit(s3_stores, world):
    """The local gateways: the platform account is another tenant there."""
    if not s3_stores.get("platform"):
        pytest.skip("SOVA_TEST_S3_PLATFORM names the other tenant's account")
    root = s3_stores["root"]("target")

    def create(bucket):
        root.create_bucket(Bucket=bucket)
        root.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Sid": "OtherTenant", "Effect": "Allow",
                "Principal": {"AWS": [s3_stores["platform_principal"]]},
                "Action": ["s3:*"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}))
    outsider = other_tenant(world, lambda: s3_stores["platform"]("target"), create)
    return object_probes(world, outsider=outsider,
                         anonymous=lambda side: unsigned(world.root[side]))


def unsigned(client):
    import boto3
    import botocore.config
    from botocore import UNSIGNED

    return boto3.client("s3", endpoint_url=client.meta.endpoint_url, region_name="us-east-1",
                        config=botocore.config.Config(signature_version=UNSIGNED,
                                                      retries={"max_attempts": 1}))


def run(h, world, probes):
    doc = json.loads(h["revision"].canonical)
    return drive(h, h["revision"], h["plan"], adapter=world.adapter(), probes=None,
                 other_db=None, scratch_db=None, operation="object-handoff",
                 approval_id="approval-1", measures=measurements(probes, doc))


def test_an_object_storage_unit_goes_through_the_same_protocol(postgres_database, s3_stores):
    h = admitted_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1),
                         adjust=object_controls(*LOCAL_CONTROLS))
    world = OrdersInObjects(s3_stores, adapter_id=h["world"]["manifest"].adapter_id)
    record, observed = run(h, world, local_unit(s3_stores, world))
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, record.held_from, observed.get("uncertain"), observed.get("failed"))
    assert record.owner_environment == "onprem-target"
    assert world.route() == world.route_of("target")
    for name in DATA_SETS:
        assert world.prefix_digest("source", name + "/") == world.prefix_digest(
            "target", name + "/")
    assert world.can_write("target", "app") and not world.can_write("source", "app")
    assert not world.can_write("target", "other")  # only the application was activated


def test_a_business_invariant_broken_in_the_objects_stops_before_activation(
        postgres_database, s3_stores):
    """The copy is faithful, yet one order has no recorded result: ORDER-01 fails
    on the target before activation and the handoff is held, the source intact."""
    h = admitted_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1),
                         adjust=object_controls(*LOCAL_CONTROLS))
    world = OrdersInObjects(s3_stores, adapter_id=h["world"]["manifest"].adapter_id)
    world.root["source"].delete_object(Bucket=world.buckets["source"],
                                       Key="idempotency_results/c-1/key-4.json")
    record, observed = run(h, world, local_unit(s3_stores, world))
    assert record.state == HandoffState.SAFE_HOLD and record.reason == "CONTRACT_BREACH"
    assert not record.activation_possible  # held before the target could take writes
    assert [oid for oid, _ in observed["failed"]] == ["business.ORDER-01"]
    assert world.route() == world.route_of("source")


def on_premises(s3_stores, store):
    """The local source gateway, and a SeaweedFS store as the on-premises
    target (its own accounts, its own principal names)."""
    client = store["client"]
    return {**s3_stores,
            "root": lambda side: (s3_stores["root"](side) if side == "source"
                                  else client("admin")),
            "as": lambda side, writer: (s3_stores["as"](side, writer) if side == "source"
                                        else client(writer)),
            "principals": {"source": s3_stores["principals"]["source"],
                           "target": {w: principal(w) for w in ("app", "other")}},
            "platform": None}


def on_premises_unit(s3_stores, store, h):
    world = OrdersInObjects(on_premises(s3_stores, store),
                            adapter_id=h["world"]["manifest"].adapter_id)
    admin = store["client"]("admin")
    outsider = other_tenant(world, lambda: store["client"]("outsider"),
                            lambda bucket: admin.create_bucket(Bucket=bucket))
    tls = ({"target": TlsEndpoint("127.0.0.1", store["port"], "localhost", store["ca_pem"])}
           if "ca_pem" in store else None)
    probes = object_probes(world, outsider=outsider, tls=tls,
                           anonymous=lambda side: store["client"](None) if side == "target"
                           else None)
    return world, probes


@pytest.mark.seaweedfs
def test_a_locked_on_premises_target_takes_the_unit(postgres_database, s3_stores,
                                                    seaweedfs_stores):
    """Cloud-style source, SeaweedFS locked with identities and TLS as the
    target: every target control is measured and passes, and the unit moves."""
    h = admitted_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1),
                         adjust=object_controls(*ON_PREMISES_CONTROLS))
    world, probes = on_premises_unit(s3_stores, seaweedfs_stores["locked"], h)
    record, observed = run(h, world, probes)
    assert record.state == HandoffState.HANDOFF_COMPLETE, (
        record.reason, record.held_from, observed.get("uncertain"), observed.get("failed"))
    assert world.route() == world.route_of("target")
    assert world.can_write("target", "app") and not world.can_write("source", "app")
    assert not world.can_write("target", "other")


@pytest.mark.seaweedfs
def test_an_on_premises_target_left_open_holds_the_handoff_before_writes_pause(
        postgres_database, s3_stores, seaweedfs_stores):
    """The same unit onto SeaweedFS as it starts by default: the target
    controls fail where they are measured, before writes pause, so the
    application never stops writing to the source and nothing moves."""
    h = admitted_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1),
                         adjust=object_controls(*ON_PREMISES_CONTROLS))
    world, probes = on_premises_unit(s3_stores, seaweedfs_stores["open"], h)
    record, observed = run(h, world, probes)
    assert record.state == HandoffState.SAFE_HOLD and record.reason == "CONTRACT_BREACH"
    assert not record.activation_possible
    failed = dict(observed["failed"])
    assert set(failed) == {"security.deny_anonymous_data_access",
                           "security.deny_cross_tenant_data_access",
                           "security.verified_transport_encryption",
                           "security.refuse_unverified_transport"}
    assert failed["security.deny_anonymous_data_access"]["outcome"] == "allowed"
    assert failed["security.deny_cross_tenant_data_access"]["outcome"] == "allowed"
    assert world.can_write("source", "app")  # admission was never paused
    assert world.route() == world.route_of("source")
