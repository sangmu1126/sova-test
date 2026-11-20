from datetime import datetime, timezone, timedelta
import ast
import base64
from pathlib import Path
import pytest
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "personal_qualification_wireguard_endpoint",
    Path(__file__).resolve().parents[2]
    / "infra/personal-journey/wireguard_endpoint.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
expired_archive_suffix = _MODULE.expired_archive_suffix

NOW = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)


def state():
    return {
        "run_id": "b0be270e62",
        "side": "target",
        "applied": False,
        "private_key_persistent": False,
        "expires_at": (NOW - timedelta(hours=2)).isoformat(),
        "removed_at": (NOW - timedelta(hours=2) + timedelta(seconds=1)).isoformat(),
        "public_key": base64.b64encode(b"a" * 32).decode(),
    }


def test_expired_removed_public_endpoint_is_archivable():
    assert expired_archive_suffix(state(), "target", NOW).startswith("-expired-")


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "other"},
        {"side": "source"},
        {"applied": True},
        {"private_key_persistent": True},
        {"expires_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"removed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"public_key": "bad"},
    ],
)
def test_unsettled_changed_or_still_valid_endpoint_is_not_archivable(change):
    s = state()
    s.update(change)
    with pytest.raises(ValueError):
        expired_archive_suffix(s, "target", NOW)


def test_embedded_operator_programs_parse_without_executing():
    source = (
        Path(__file__).resolve().parents[2]
        / "infra/personal-journey/personal_wireguard.py"
    ).read_text()
    tree = ast.parse(source)
    programs = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("import ", "from "))
        and "\n" in node.value
    ]
    assert len(programs) >= 5
    for p in programs:
        compile(p, "<bounded-private-program>", "exec")
