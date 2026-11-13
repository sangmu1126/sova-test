"""Reviewing a frozen plan before approval: verify, then explain (§24.2)."""

import json

from contract_world import build_world
import pytest

from sova.domain.plan_compiler import compile_revision
from sova.interfaces.cli import main


@pytest.fixture
def plan_file(now, tmp_path):
    world = build_world(now)
    for subject in (world["contract"], world["snapshot"], world["qualification"]):
        world["sign"](subject)
    revision, result = compile_revision(
        world["contract"], world["snapshot"], requested_nodes=("api-a", "api-b", "database"),
        expected_tenant=world["contract"].tenant_id, source=world["source"],
        target=world["target"], manifest=world["manifest"],
        qualification=world["qualification"], rows=2000, authenticate=world["auth"],
        clock=lambda: now,
    )
    path = tmp_path / "plan.json"
    path.write_text(revision.canonical + "\n")
    return path, revision


def test_explains_a_verified_plan(plan_file, capsys):
    path, revision = plan_file
    assert main(["plan-explain", str(path), "--expect-digest", revision.digest]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["compiled_digest"] == revision.digest
    assert out["authorizes_execution"] is False
    assert out["point_of_no_return"] == "record_activation_intent"
    kinds = [s["kind"] for s in out["steps"]]
    assert kinds[0] == "stage_target" and kinds[-1] == "stabilize"
    fence = next(s for s in out["steps"] if s["kind"] == "fence_source_writes")
    assert fence["acts_on"] == "source"
    assert fence["evidence"] == [{"id": "fence.source_writes_denied",
                                  "kind": "independent_write_denial_probe",
                                  "gate": "PREFLIGHT"}]
    assert {"key": ["source", "synthetic-source", "synthetic-uid-database"],
            "mode": "write"} in out["claims"]
    assert any(d["level"] == "PLATFORM_EXIT_VERIFIED" for d in out["independence_drills"])
    assert out["failback_of"] is None


def test_refuses_a_plan_that_is_not_the_one_under_review(plan_file, capsys, tmp_path):
    path, revision = plan_file
    assert main(["plan-explain", str(path), "--expect-digest", "0" * 64]) == 2
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(json.loads(revision.canonical), indent=2))
    assert main(["plan-explain", str(pretty)]) == 2  # same content, not canonical bytes
    edited = tmp_path / "edited.json"
    edited.write_text(revision.canonical.replace("offline-copy", "offline-c0py"))
    assert main(["plan-explain", str(edited), "--expect-digest", revision.digest]) == 2
    capsys.readouterr()
