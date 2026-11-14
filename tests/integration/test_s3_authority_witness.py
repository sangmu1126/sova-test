"""Authority witness on a locally run S3-compatible store with Object Lock.

The store enforces compliance-mode retention itself: these tests try to
delete, shorten, hide and rewrite witnessed entries for real. Every entry is
locked for an hour in a disposable container; nothing outside it is touched.
"""

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

from botocore.exceptions import ClientError
from handoff_support import admitted_handoff
import psycopg
from psycopg.types.json import Jsonb
import pytest

from sova.adapters.s3_authority_witness import S3ObjectLockAuthorityWitness
from sova.domain.contracts import ContractError
from sova.domain.handoff_authority import (
    ROLLBACK_RECOVERY,
    AuthorityRollbackDetected,
    EffectOutcome,
    HandoffState,
)

pytestmark = pytest.mark.s3
APP, UNIT = "orders", "a" * 64
HOUR = timedelta(hours=1)


@pytest.fixture
def store(s3_stores):
    client = s3_stores["root"]("target")
    bucket = "witness-" + uuid4().hex[:12]
    client.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    return client, bucket


def witness(store, **kwargs):
    client, bucket = store
    w = S3ObjectLockAuthorityWitness(client, bucket, tenant_id="tenant-a", retention=HOUR,
                                     **kwargs)
    w.require_locked_bucket()
    return w


def versions(store):
    client, bucket = store
    return client.list_object_versions(Bucket=bucket).get("Versions", [])


def test_entries_chain_and_cannot_be_deleted_or_shortened(store):
    w = witness(store)
    assert w.latest(APP, UNIT) is None
    for revision in (1, 2, 3):
        w.append(APP, UNIT, "transition", {"revision": revision})
    assert w.latest(APP, UNIT) == {"revision": 3}
    client, bucket = store
    first = sorted(versions(store), key=lambda v: v["Key"])[0]
    with pytest.raises(ClientError, match="AccessDenied"):
        client.delete_object(Bucket=bucket, Key=first["Key"], VersionId=first["VersionId"])
    with pytest.raises(ClientError, match="AccessDenied"):
        client.put_object_retention(
            Bucket=bucket, Key=first["Key"], VersionId=first["VersionId"],
            Retention={"Mode": "COMPLIANCE",
                       "RetainUntilDate": datetime.now(UTC) + timedelta(minutes=1)})
    assert w.latest(APP, UNIT) == {"revision": 3}


def test_a_hidden_tail_stops_the_witness(store):
    w = witness(store)
    w.append(APP, UNIT, "opened", {"revision": 1})
    w.append(APP, UNIT, "transition", {"revision": 2})
    client, bucket = store
    latest = sorted(v["Key"] for v in versions(store))[-1]
    client.delete_object(Bucket=bucket, Key=latest)  # a delete marker, not a deletion
    with pytest.raises(ContractError, match="hidden"):
        w.latest(APP, UNIT)
    with pytest.raises(ContractError, match="hidden"):
        w.append(APP, UNIT, "transition", {"revision": 3})


def test_a_rewritten_entry_stops_the_witness(store):
    w = witness(store)
    w.append(APP, UNIT, "opened", {"revision": 1})
    client, bucket = store
    key = versions(store)[0]["Key"]
    client.put_object(Bucket=bucket, Key=key, Body=b'{"record": {"revision": 9}}')
    with pytest.raises(ContractError, match="rewritten"):
        w.latest(APP, UNIT)


def test_a_concurrent_append_is_refused_not_overwritten(store):
    first, second = witness(store), witness(store)
    first.append(APP, UNIT, "opened", {"revision": 1})
    original = first._chain

    def raced(prefix):
        chain = original(prefix)
        second.append(APP, UNIT, "transition", {"revision": 2, "by": "second"})
        return chain

    first._chain = raced
    with pytest.raises(ContractError, match="append refused"):
        first.append(APP, UNIT, "transition", {"revision": 2, "by": "first"})
    assert witness(store).latest(APP, UNIT) == {"revision": 2, "by": "second"}


def test_unlocked_storage_or_entries_are_not_a_witness(s3_stores, store):
    client = s3_stores["root"]("target")
    plain = "plain-" + uuid4().hex[:12]
    client.create_bucket(Bucket=plain)
    with pytest.raises(ContractError, match="without Object Lock"):
        S3ObjectLockAuthorityWitness(client, plain, tenant_id="tenant-a",
                                     retention=HOUR).require_locked_bucket()
    w = witness(store)
    w.append(APP, UNIT, "opened", {"revision": 1})
    chain, previous = w._chain(w._prefix(APP, UNIT))
    _, bucket = store
    client.put_object(  # a well-formed next entry, written without a lock
        Bucket=bucket, Key=w._prefix(APP, UNIT) + "000000000002.json",
        Body=json.dumps({"tenant_id": "tenant-a", "seq": 2, "prev": previous,
                         "record": {"revision": 2}}).encode())
    with pytest.raises(ContractError, match="not locked"):
        w.latest(APP, UNIT)


def test_a_latest_entry_past_its_retention_is_not_trusted(store):
    w = witness(store)
    w.append(APP, UNIT, "opened", {"revision": 1})
    later = witness(store, clock=lambda: datetime.now(UTC) + HOUR + timedelta(minutes=5))
    with pytest.raises(ContractError, match="no longer locked"):
        later.latest(APP, UNIT)
    with pytest.raises(ContractError):
        S3ObjectLockAuthorityWitness(store[0], store[1], tenant_id="tenant-a",
                                     retention=timedelta(minutes=5))


@pytest.mark.postgres
def test_a_restored_authority_database_is_caught_by_the_locked_witness(
        postgres_database, now, store):
    """AT-33 with the witness on locked object storage instead of a file."""
    h = admitted_handoff(postgres_database, now, witness_for=lambda tenant: (
        S3ObjectLockAuthorityWitness(store[0], store[1], tenant_id=tenant, retention=HOUR)))
    h["coordinator"].open(h["revision"], h["plan"], actor_id="coordinator")
    for op in h["ops"]:
        if op["kind"] == "copy_content":
            break
        for obligation_id in op["required_evidence"]:
            h["prove"](obligation_id)
        h["coordinator"].apply_effect(h["revision"], h["plan"],
                                      operation_id=op["operation_id"],
                                      outcome=EffectOutcome.EFFECT_CONFIRMED,
                                      actor_id="coordinator")
    unit = json.loads(h["revision"].canonical)["unit"]["unit_digest"]
    latest = h["witness"].latest(h["app"], unit)
    old = next(r for rev, _, _, r in h["store"].history(h["app"], unit) if rev == 2)
    with psycopg.connect(postgres_database[0]) as conn:  # point-in-time restore
        conn.execute(
            "UPDATE sova_control.handoff_authority SET revision=%s,state=%s,document=%s "
            "WHERE tenant_id=%s AND unit_digest=%s",
            (old.revision, str(old.state), Jsonb(old.document), h["tenant"], unit))
    with pytest.raises(AuthorityRollbackDetected):
        h["coordinator"].next_command(h["revision"], h["plan"], operation_id="op", epoch=1)
    recovered = h["store"].recover(h["app"], unit, h["revision"], actor_id="recovery")
    assert recovered.state == HandoffState.SAFE_HOLD and recovered.reason == ROLLBACK_RECOVERY
    assert recovered.epoch == latest["epoch"] + 1
    assert h["witness"].latest(h["app"], unit) == recovered.document
