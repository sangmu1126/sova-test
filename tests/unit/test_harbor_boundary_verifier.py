"""Host-side verifier safety checks; no Harbor/AI/deployment claim."""

import pytest

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "personal_operator_harbor_boundary_verifier",
    Path(__file__).resolve().parents[2]
    / "infra/personal-journey/harbor_boundary_verifier.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
read_regular = _MODULE.read_regular
verify = _MODULE.verify


REFERENCE = Path(__file__).resolve().parents[2] / "src/sova/service/http_boundary.py"


def test_reference_passes_ten_behavioral_checks(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(REFERENCE.read_bytes())
    result = verify(candidate, REFERENCE)
    assert result["passed"] and len(result["checks"]) == 10
    assert result["operational_qualification"] is False


@pytest.mark.parametrize(
    "source",
    (b"raise AssertionError('must never execute')\n", b"import os;os._exit(99)\n"),
)
def test_unreviewed_candidate_is_rejected_before_execution(tmp_path, source):
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(source)
    assert verify(candidate, REFERENCE) == {
        "passed": False,
        "reason": "candidate_outside_reviewed_patch",
        "candidate_executed": False,
    }


def test_known_aggregate_bypass_is_not_an_acceptable_patch(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(
        REFERENCE.read_bytes().replace(b"len(data) + len(chunk)", b"len(chunk)")
    )
    assert verify(candidate, REFERENCE)["passed"] is False


def test_symlink_and_oversized_artifacts_are_refused(tmp_path):
    link = tmp_path / "link.py"
    link.symlink_to(REFERENCE)
    with pytest.raises(OSError):
        read_regular(link)
    large = tmp_path / "large.py"
    large.write_bytes(b"x" * 16385)
    with pytest.raises(ValueError):
        read_regular(large)
