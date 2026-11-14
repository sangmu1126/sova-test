"""The same compiled handoff, with object storage as the data: the protocol does not change.

The plan, its approval, admission, the authority record, the effect ledger and
the signed evidence are exactly those of the PostgreSQL handoffs; only the
adapter (S3-compatible offline copy between two independent stores) and the
measurements behind each obligation differ. The unit's data sets become object
prefixes: ``data.orders`` compares everything under ``orders/``, ORDER-01 is
checked on the order and result objects, residency on the target bucket's
zone tag, the restore test by restoring the target into a fresh bucket.
"""

from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from botocore.exceptions import ClientError
from handoff_drive import drive, without_transport_control
from handoff_support import admitted_handoff
import pytest
from s3_world import S3World

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


def measurements(world, plan_doc):
    contract_zones = {ZONE}

    def placement(_):
        tags = world.root["target"].get_bucket_tagging(Bucket=world.buckets["target"])
        label = {t["Key"]: t["Value"] for t in tags["TagSet"]}.get("zone")
        return label in contract_zones, {"probe": "zone", "label": label}

    def source_denied(_):
        denied = {w: not world.can_write("source", w) for w in ("app", "other")}
        return all(denied.values()), {"probe": "write_denied", "source": denied}

    def content(name):
        def measure(_):
            source, target = (world.prefix_digest(s, name + "/") for s in ("source", "target"))
            return source == target and source["objects"] > 0, {
                "probe": "content_equal", "prefix": name, "source": source, "target": target}
        return measure

    def order_effects_once(_):
        client, bucket = world.root["target"], world.buckets["target"]
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                  Prefix="orders/")
        seen, unrecorded = {}, 0
        for key in sorted(o["Key"] for p in pages for o in p.get("Contents", ())):
            order = json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
            if "idempotency_key" not in order:
                continue  # business writes made during the handoff carry no key yet
            pair = (order["customer"], order["idempotency_key"])
            seen[pair] = seen.get(pair, 0) + 1
            try:
                client.head_object(Bucket=bucket,
                                   Key=f"idempotency_results/{pair[0]}/{pair[1]}.json")
            except ClientError:
                unrecorded += 1
        duplicates = sum(1 for n in seen.values() if n > 1)
        return not duplicates and not unrecorded and bool(seen), {
            "probe": "order_effects_once", "orders": len(seen), "duplicates": duplicates,
            "unrecorded": unrecorded}

    def single_writer(_):
        target = world.can_write("target", "app")
        denied, observation = source_denied(None)
        return target and denied, {"probe": "single_writer", "target": target,
                                   "source": observation["source"]}

    def outsider_denied(_):
        outsider = world.s["platform"]("target") if world.s.get("platform") else None
        if outsider is None:
            return False, {"probe": "cross_tenant_denied", "outcome": "no outsider account"}
        try:
            outsider.list_objects_v2(Bucket=world.buckets["target"], MaxKeys=1)
            return False, {"probe": "cross_tenant_denied", "outcome": "listed"}
        except ClientError as error:
            code = error.response["Error"]["Code"]
            return code == "AccessDenied", {"probe": "cross_tenant_denied", "outcome": code}

    def restore(_):
        client, scratch = world.root["target"], "restore-" + uuid4().hex[:10]
        client.create_bucket(Bucket=scratch)
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=world.buckets["target"])
        for key in sorted(o["Key"] for p in pages for o in p.get("Contents", ())):
            client.copy_object(Bucket=scratch, Key=key,
                               CopySource={"Bucket": world.buckets["target"], "Key": key})
        original = {p: world.prefix_digest("target", p + "/") for p in DATA_SETS}
        world.buckets["scratch"], world.root["scratch"] = scratch, client
        restored = {p: world.prefix_digest("scratch", p + "/") for p in DATA_SETS}
        return original == restored, {"probe": "restore_drill",
                                      "equal": {p: original[p] == restored[p] for p in DATA_SETS}}

    measures = {"business.ORDER-01": order_effects_once,
                "fence.source_writes_denied": source_denied,
                "route.stale_writes_rejected": source_denied,
                "authority.single_writer_after_activation": single_writer,
                "security.deny_cross_tenant_data_access": outsider_denied,
                "security.tested_restore": restore, "operations.restore_test": restore}
    for o in plan_doc["obligations"]:
        if o["kind"] == "placement_evidence":
            measures[o["id"]] = placement
        elif o["kind"] == "final_content_comparison":
            measures[o["id"]] = content(o["id"].split(".", 1)[1])
    return measures


def test_an_object_storage_unit_goes_through_the_same_protocol(postgres_database, s3_stores):
    h = admitted_handoff(postgres_database, datetime.now(UTC) - timedelta(seconds=1),
                         adjust=without_transport_control)
    doc = json.loads(h["revision"].canonical)
    world = OrdersInObjects(s3_stores, adapter_id=h["world"]["manifest"].adapter_id)
    record, observed = drive(h, h["revision"], h["plan"], adapter=world.adapter(), probes=None,
                             other_db=None, scratch_db=None, operation="object-handoff",
                             approval_id="approval-1", measures=measurements(world, doc))
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
                         adjust=without_transport_control)
    doc = json.loads(h["revision"].canonical)
    world = OrdersInObjects(s3_stores, adapter_id=h["world"]["manifest"].adapter_id)
    world.root["source"].delete_object(Bucket=world.buckets["source"],
                                       Key="idempotency_results/c-1/key-4.json")
    record, observed = drive(h, h["revision"], h["plan"], adapter=world.adapter(), probes=None,
                             other_db=None, scratch_db=None, operation="object-handoff",
                             approval_id="approval-1", measures=measurements(world, doc))
    assert record.state == HandoffState.SAFE_HOLD and record.reason == "CONTRACT_BREACH"
    assert not record.activation_possible  # held before the target could take writes
    assert [oid for oid, _ in observed["failed"]] == ["business.ORDER-01"]
    assert world.route() == world.route_of("source")
