"""Registry documents with revisions on real PostgreSQL: compare-and-set, an
immutable history, tenant isolation, and exactly one winner among writers
that read the same revision."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

from contract_world import build_world
import psycopg
import pytest

from sova.adapters.postgres_registrations import PostgresRegistrations, RegistrationConflict
from sova.adapters.registration import registry_codec as codecs
from sova.application.service_contracts import example_service_contract
from sova.domain.contracts import ContractError
from sova.domain.service_contracts import ServiceContract

pytestmark = pytest.mark.postgres


@pytest.fixture
def store(postgres_database, now):
    dsn, role = postgres_database
    tenant = "tenant-" + uuid4().hex  # each test its own tenant: rows outlive a test
    data = example_service_contract().document
    data["metadata"]["tenant_id"] = tenant
    world = build_world(now, contract=ServiceContract.from_document(data))

    def connect():
        return psycopg.connect(dsn, user=role)

    return {"world": world, "registry": PostgresRegistrations(connect, tenant_id=tenant),
            "connect": connect, "tenant": tenant, "admin": dsn}


def profile(world, revision):
    return replace(world["target"], revision=revision)


def test_revisions_move_only_from_the_revision_read(store):
    r, p1 = store["registry"], profile(store["world"], 1)
    subject = p1.profile_id
    first = r.register("environment_profile", subject, codecs.encode(p1), expected_revision=0,
                       registered_by="registrar-a")
    assert first.revision == 1 and first.value == p1
    p2 = profile(store["world"], 2)
    second = r.register("environment_profile", subject, codecs.encode(p2), expected_revision=1,
                        registered_by="registrar-b")
    assert second.revision == 2 and r.current("environment_profile", subject).value == p2
    with pytest.raises(RegistrationConflict) as stale:  # written from an old read
        r.register("environment_profile", subject, codecs.encode(profile(store["world"], 3)),
                   expected_revision=1, registered_by="registrar-c")
    assert stale.value.current_revision == 2
    assert [h["revision"] for h in r.history("environment_profile", subject)] == [1, 2]
    assert r.at("environment_profile", subject, 1).value == p1  # history unchanged


def test_a_retried_write_returns_what_it_wrote(store):
    r, p1 = store["registry"], profile(store["world"], 1)
    first = r.register("environment_profile", p1.profile_id, codecs.encode(p1),
                       expected_revision=0, registered_by="registrar-a")
    again = r.register("environment_profile", p1.profile_id, codecs.encode(p1),
                       expected_revision=0, registered_by="registrar-a")
    assert again == first


def test_exactly_one_of_two_writers_from_the_same_read_wins(store):
    r, world = store["registry"], store["world"]
    subject = world["source"].profile_id
    r.register("environment_profile", subject, codecs.encode(world["source"]),
               expected_revision=0, registered_by="registrar-a")
    barrier = Barrier(2)

    def write(revision):
        barrier.wait()
        try:
            return r.register("environment_profile", subject,
                              codecs.encode(replace(world["source"], revision=revision)),
                              expected_revision=1, registered_by=f"writer-{revision}")
        except RegistrationConflict as conflict:
            return conflict

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, (5, 6)))
    winners = [x for x in results if not isinstance(x, RegistrationConflict)]
    losers = [x for x in results if isinstance(x, RegistrationConflict)]
    assert len(winners) == 1 and len(losers) == 1 and losers[0].current_revision == 2
    assert r.current("environment_profile", subject).digest == winners[0].digest


def test_documents_must_name_their_subject_and_tenant(store):
    r, world = store["registry"], store["world"]
    with pytest.raises(ContractError, match="another subject"):
        r.register("environment_profile", "other-profile", codecs.encode(world["source"]),
                   expected_revision=0, registered_by="registrar-a")
    foreign = PostgresRegistrations(store["connect"], tenant_id="tenant-" + uuid4().hex)
    with pytest.raises(ContractError, match="another tenant"):
        foreign.register("service_contract", world["contract"].service_id,
                         codecs.encode(world["contract"]), expected_revision=0,
                         registered_by="registrar-a")


def test_tenants_see_only_their_own_registrations(store):
    r, world = store["registry"], store["world"]
    r.register("service_contract", world["contract"].service_id,
               codecs.encode(world["contract"]), expected_revision=0, registered_by="r")
    other = PostgresRegistrations(store["connect"], tenant_id="tenant-" + uuid4().hex)
    assert other.current("service_contract", world["contract"].service_id) is None
    assert other.subjects("service_contract") == []


def test_the_history_cannot_be_rewritten_by_the_runtime_role(store):
    r, world = store["registry"], store["world"]
    r.register("environment_profile", world["source"].profile_id,
               codecs.encode(world["source"]), expected_revision=0, registered_by="r")
    with r.transaction() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE sova_control.registrations SET registered_by='x'")
    with r.transaction() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM sova_control.registration_heads")
