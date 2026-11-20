"""Storage identity chain refusal tests; synthetic inputs are not live evidence."""

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "personal_placement_collector",
    Path(__file__).resolve().parents[2] / "infra/personal-journey/collect_placement_facts.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def chain():
    m = _MODULE
    source = {"filesystem": {"source": "/dev/nvme0n1p1"}, "devices": [
        {"name": "nvme0n1", "serial": m.VOLUME.replace("-", ""), "children": [
            {"name": "nvme0n1p1", "pkname": "nvme0n1"}]}]}
    target = {"machine_id": m.MACHINE, "cluster_uid": m.CLUSTER, "namespace_uid": m.NAMESPACE_UID,
              "pv_path": m.MOUNT, "pvc_phase": "Bound", "pvc_uid": "pvc-synthetic",
              "filesystem": {"source": "/dev/mapper/sova-" + m.RUN, "target": m.MOUNT},
              "files": [{"category": "backups", "path": m.BACKUP, "sha256": m.BACKUP_SHA, "device": 7},
                        {"category": "audit", "path": m.MOUNT + "/authority-witness/personal-qualification.log",
                         "sha256": "f" * 64, "device": 7}]}
    volume = {"VolumeId": m.VOLUME, "Encrypted": True,
              "Attachments": [{"InstanceId": m.INSTANCE, "State": "attached"}]}
    databases = {s: {"system_identifier": system, "tls": True,
                     "database": s + "_orders", "data_directory": "/var/lib/postgresql/data" +
                     ("/pgdata" if s == "target" else "")} for s, system in m.SYSTEMS.items()}
    return source, target, volume, databases


def test_separate_observed_assets_never_become_residency_or_migration_pass():
    report = _MODULE.summarize(*chain())
    assert all(report[c]["observed"] is True for c in ("business_data", "backups", "audit"))
    assert report["ai_inputs"]["observed"] is False
    assert report["audit"]["all_audit_sinks_observed"] is False
    assert report["backups"]["restore_retested"] is False
    assert all(report[k] is False for k in (
        "all_copies_enumerated", "geographic_residency_verified",
        "residency_obligation_satisfied", "migration_authorized"))


@pytest.mark.parametrize("index,path,value", [
    (2, ("VolumeId",), "vol-foreign"),
    (2, ("Encrypted",), False),
    (2, ("Attachments", 0, "InstanceId"), "i-foreign"),
    (2, ("Attachments", 0, "State"), "detached"),
    (0, ("devices", 0, "serial"), "vol-foreign"),
    (0, ("devices", 0, "children", 0, "pkname"), "nvme1n1"),
    (0, ("filesystem", "source"), "/dev/nvme1n1p1"),
    (1, ("machine_id",), "another-vm"),
    (1, ("cluster_uid",), "another-cluster"),
    (1, ("namespace_uid",), "recreated-namespace"),
    (1, ("pvc_phase",), "Pending"),
    (1, ("filesystem", "source"), "/dev/vda1"),
    (1, ("pv_path",), "/tmp/unprotected"),
    (1, ("files", 0, "sha256"), "0" * 64),
    (1, ("files", 1, "category"), "backups"),
    (1, ("files", 1, "device"), 8),
    (3, ("target", "system_identifier"), "another-server"),
    (3, ("source", "tls"), False),
    (3, ("target", "database"), "another_database"),
    (3, ("source", "data_directory"), "/tmp/unrelated"),
])
def test_wrong_asset_rebound_storage_or_category_substitution_is_refused(index, path, value):
    inputs = list(deepcopy(chain()))
    cursor = inputs[index]
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ValueError):
        _MODULE.summarize(*inputs)
