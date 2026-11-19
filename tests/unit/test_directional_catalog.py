import json

import pytest

from sova.domain.contracts import ContractError
from sova.domain.plan_compiler import PlanRevision
from sova.service.directional_catalog import DirectionalCatalog, catalog_v2, source_identity
from sova.service.worker import HandoffWorker


def revision(code, *, source="aws-personal", target="local-vm", tenant="personal", app="orders", adapter="pg"):
    return PlanRevision(json.dumps({"tenant_id": tenant, "service_id": app,
        "environments": {"source": source, "target": target},
        "operations": [{"adapter": {"adapter_id": adapter, "adapter_digest": code}}, {"adapter": None}]}))


def test_exact_direction_is_selected_without_guessing_reverse_databases():
    seen = []
    code = "a" * 64

    def factory(direction):
        def run(*args):
            seen.append((direction, args))
            return direction
        return run

    catalog = DirectionalCatalog({
        ("personal", "orders", "pg", "aws-personal", "local-vm"): factory("forward"),
        ("personal", "orders", "pg", "local-vm", "aws-personal"): factory("reverse")},
        expected_digest=code, measure=lambda: {"digest": code})
    assert catalog.build("personal", "orders", "pg", revision(code)) == "forward"
    assert catalog.build("personal", "orders", "pg", revision(code, source="local-vm", target="aws-personal")) == "reverse"
    assert len(seen) == 2


@pytest.mark.parametrize("changes", [
    {"source": "foreign"}, {"target": "foreign"}, {"tenant": "foreign"},
    {"app": "foreign"}, {"adapter": "foreign"}, {"code": "b" * 64}])
def test_mismatched_plan_never_calls_a_factory(changes):
    calls = []
    code = "a" * 64
    catalog = DirectionalCatalog({("personal", "orders", "pg", "aws-personal", "local-vm"): lambda *a: calls.append(a)},
        expected_digest=code, measure=lambda: {"digest": code})
    with pytest.raises(ContractError):
        catalog.build("personal", "orders", "pg", revision(**{"code": code, **changes}))
    assert not calls


def test_changed_source_after_start_is_refused_before_factory():
    current, calls = ["a" * 64], []
    catalog = DirectionalCatalog({("personal", "orders", "pg", "aws-personal", "local-vm"): lambda *a: calls.append(a)},
        expected_digest=current[0], measure=lambda: {"digest": current[0]})
    current[0] = "b" * 64
    with pytest.raises(ContractError, match="implementation differs"):
        catalog.build("personal", "orders", "pg", revision("a" * 64))
    assert not calls


def test_cached_execution_does_not_skip_the_next_step_code_check():
    current, calls = ["a" * 64], []
    worker = object.__new__(HandoffWorker)
    worker.catalog = DirectionalCatalog({("personal", "orders", "pg", "aws-personal", "local-vm"): lambda *a: calls.append(a)},
        expected_digest=current[0], measure=lambda: {"digest": current[0]})
    worker._executor = lambda *args: (_ for _ in ()).throw(AssertionError("Must not create an effect journal"))
    frozen = revision(current[0])
    doc = json.loads(frozen.canonical)
    current[0] = "b" * 64
    with pytest.raises(ContractError, match="implementation differs"):
        worker._step("personal", "orders", None, None, frozen, doc, None,
                     None, None, None, None, doc["operations"][0], {}, None, object())
    assert not calls


def test_source_identity_includes_sql_and_rejects_symlinks(tmp_path):
    (tmp_path / "one.py").write_text("one")
    (tmp_path / "two.sql").write_text("two")
    first = source_identity(tmp_path)
    assert set(first["files"]) == {"one.py", "two.sql"}
    (tmp_path / "two.sql").write_text("changed")
    assert source_identity(tmp_path)["digest"] != first["digest"]
    before_policy = source_identity(tmp_path)["digest"]
    (tmp_path / "policy.json").write_text('{"enforced":true}')
    assert source_identity(tmp_path)["digest"] != before_policy
    (tmp_path / "compat.sql").symlink_to(tmp_path / "two.sql")
    assert source_identity(tmp_path)["files"]["compat.sql"] == source_identity(tmp_path)["files"]["two.sql"]
    (tmp_path / "link.py").symlink_to(tmp_path / "one.py")
    with pytest.raises(ContractError, match="symlink"):
        source_identity(tmp_path)


def test_invalid_later_entry_closes_earlier_catalog():
    closed = []

    class Inner:
        def build(self, *args):
            raise AssertionError("Configuration must not execute")

    app = {"tenant_id": "personal", "application_id": "orders", "adapter_id": "pg",
           "environment_pair": {"source": "aws-personal", "target": "local-vm"}}
    with pytest.raises(ContractError, match="registered twice"):
        catalog_v2({"version": 2, "implementation_digest": source_identity()["digest"],
            "applications": [app, app]}, catalog_builder=lambda _: (Inner(), lambda: closed.append(True)))
    assert closed == [True]


def test_missing_direction_never_opens_adapter_resources():
    calls = []
    with pytest.raises(ContractError, match="Distinct registered"):
        catalog_v2({"version": 2, "implementation_digest": source_identity()["digest"],
            "applications": [{"tenant_id": "personal", "application_id": "orders", "adapter_id": "pg"}]},
            catalog_builder=lambda d: calls.append(d))
    assert not calls


def test_v2_keeps_the_existing_factory_and_rejects_source_mismatch():
    seen, closed = [], []
    code = source_identity()["digest"]

    class Inner:
        def build(self, *args):
            seen.append(args)
            return "existing-real-factory"

    app = {"tenant_id": "personal", "application_id": "orders", "adapter_id": "pg",
        "environment_pair": {"source": "aws-personal", "target": "local-vm"}}
    catalog, close = catalog_v2({"version": 2, "implementation_digest": code, "applications": [app]},
        catalog_builder=lambda d: (Inner(), lambda: closed.append(True)))
    assert catalog.build("personal", "orders", "pg", revision(code)) == "existing-real-factory"
    close()
    assert len(seen) == 1 and closed == [True]
    with pytest.raises(ContractError, match="implementation differs"):
        catalog_v2({"version": 2, "implementation_digest": "f" * 64, "applications": [app]},
            catalog_builder=lambda d: (Inner(), lambda: closed.append(True)))
    assert closed == [True, True]
