"""A failed SSH projection may contain partial auth stdout; never publish it."""

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_failed_remote_projection_does_not_echo_or_persist_partial_token(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "personal_vm_control_diagnostics",
        Path(__file__).resolve().parents[2] / "infra/personal-journey/connect_vm_control_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "identity.json").write_text(json.dumps({
        "run_id": "b0be270e62", "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }))
    output = tmp_path / "diagnostic"
    partial = b"must-never-echo-partial-authentication-material"

    def failed(*args, **kwargs):
        assert args[0][0] == "ssh"
        return SimpleNamespace(returncode=1, stdout=partial, stderr=b"synthetic API refusal")

    monkeypatch.setattr(module.subprocess, "run", failed)
    monkeypatch.setattr("sys.argv", ["connect_vm_control_api", "--inputs", str(inputs),
                                    "--known-hosts", str(tmp_path / "known_hosts"), "--output", str(output)])
    with pytest.raises(RuntimeError) as error:
        module.main()
    captured = capsys.readouterr()
    assert partial.decode() not in str(error.value) + captured.out + captured.err
    assert all(partial not in p.read_bytes() for p in output.iterdir())
    assert not (output / "route.token").exists()
    assert (output / "private-projection.stderr").read_bytes() == b"synthetic API refusal"
    metadata = json.loads((output / "projection-refusal.json").read_text())
    assert metadata["ready"] is False and metadata["stdout_retained"] is False
