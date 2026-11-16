import json
from pathlib import Path
from .runtime import Runtime
import pytest


def test_lost_effect_response_is_not_automatically_replayed(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "migration-settings.private.json").write_text(
        json.dumps({"vm": "registered", "source_kubeconfig": "registered"})
    )
    r = Runtime(tmp_path)
    calls = []

    def lost():
        calls.append(1)
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        r.effect("fence", lost)
    with pytest.raises(Exception):
        r.effect("fence", lambda: calls.append(2))
    assert calls == [1]
