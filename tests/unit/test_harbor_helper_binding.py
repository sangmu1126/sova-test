from datetime import UTC, datetime, timedelta
import hashlib
import json
import pytest
from sova.domain.contracts import ContractError
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "personal_qualification_harbor_helper_binding",
    Path(__file__).resolve().parents[2]
    / "infra/personal-journey/harbor_helper_binding.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
qualified_helper = _MODULE.qualified_helper

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def files(tmp_path):
    image = "sha256:" + "a" * 64
    scan = {
        "ArtifactType": "container_image",
        "Metadata": {"ImageID": image, "DiffIDs": ["sha256:" + "b" * 64]},
        "Results": [{"Target": "os"}],
        "CreatedAt": NOW.isoformat(),
    }
    db = {
        "UpdatedAt": (NOW - timedelta(hours=1)).isoformat(),
        "NextUpdate": (NOW + timedelta(hours=23)).isoformat(),
    }
    config = {"version": 1, "image_id": image}
    for kind, data in [("scan", scan), ("database", db)]:
        path = tmp_path / (kind + ".json")
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        config[kind + "_file"] = str(path)
        config[kind + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    actual = {
        "Id": image,
        "Architecture": "arm64",
        "RootFS": {"Layers": scan["Metadata"]["DiffIDs"]},
    }
    return path, config, scan, db, actual


def test_exact_helper_receipts_and_actual_layers_match(tmp_path):
    path, config, _, _, actual = files(tmp_path)
    assert qualified_helper(path, lambda _: actual, now=NOW) == config["image_id"]


@pytest.mark.parametrize(
    "change",
    [
        "finding",
        "identity",
        "layer",
        "expired",
        "changed_file",
        "symlink",
        "public",
        "future",
        "long_db",
    ],
)
def test_helper_never_substitutes_unqualified_image(tmp_path, change):
    path, config, scan, db, actual = files(tmp_path)
    if change == "finding":
        scan["Results"][0]["Vulnerabilities"] = [{"Severity": "HIGH"}]
    if change == "identity":
        actual["Id"] = "sha256:" + "c" * 64
    if change == "layer":
        actual["RootFS"]["Layers"] = []
    if change == "expired":
        db["NextUpdate"] = NOW.isoformat()
    if change == "future":
        scan["CreatedAt"] = (NOW + timedelta(seconds=1)).isoformat()
    if change == "long_db":
        db["NextUpdate"] = (NOW + timedelta(days=90)).isoformat()
    for kind, data in [("scan", scan), ("database", db)]:
        p = tmp_path / (kind + ".json")
        p.write_text(json.dumps(data))
        config[kind + "_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    path.write_text(json.dumps(config))
    if change == "changed_file":
        (tmp_path / "scan.json").write_text("{}")
    if change == "public":
        path.chmod(0o644)
    if change == "symlink":
        copy = tmp_path / "real"
        path.rename(copy)
        path.symlink_to(copy)
    with pytest.raises(ContractError):
        qualified_helper(path, lambda _: actual, now=NOW)
