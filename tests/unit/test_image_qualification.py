"""A stale image must not inherit the source checkout's test results."""

from copy import deepcopy
import hashlib
import json
import subprocess

import pytest

from sova.qualification import image


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def inventories():
    expected = {"sova": {"service/main.py": sha("current"), "adapters/sql/control.sql": sha("sql")},
                "sova_security": {"__init__.py": sha("security")}}
    versions = {"sova": "0.1.0", "sova_security": "0.11.1"}
    observed = {"packages": {p: {"version": versions[p], "files": deepcopy(v)} for p, v in expected.items()},
                "entrypoints": {"sova-control-plane": {"sova-service": "sova.service.main:main"}},
                "distributions": {"sova-control-plane": "0.1.0", "sova-security": "0.11.1"}}
    return expected, observed, versions


def test_matching_bytes_and_metadata_are_only_a_source_match(inventories):
    assert image.compare(*inventories)["status"] == "MATCH"


@pytest.mark.parametrize("case", ["missing", "extra", "changed", "sql", "version", "entrypoint"])
def test_each_independent_stale_image_condition_refuses_match(inventories, case):
    expected, observed, versions = inventories
    files = observed["packages"]["sova"]["files"]
    if case == "missing":
        del files["service/main.py"]
    elif case == "extra":
        files["obsolete.py"] = sha("stale")
    elif case == "changed":
        files["service/main.py"] = sha("old")
    elif case == "sql":
        files["adapters/sql/control.sql"] = sha("old sql")
    elif case == "version":
        observed["packages"]["sova_security"]["version"] = "0.11.0"
    else:
        observed["entrypoints"]["sova-control-plane"]["sova-service"] = "old.worker:main"
    assert image.compare(expected, observed, versions)["status"] == "MISMATCH"


@pytest.mark.parametrize("observed", [None, {}, {"packages": []},
    {"packages": {"sova": [], "sova_security": {}}, "entrypoints": {}, "distributions": {}}])
def test_malformed_inventory_never_matches(inventories, observed):
    with pytest.raises(image.ImageCheckError, match="image_inventory_invalid"):
        image.compare(inventories[0], observed, inventories[2])


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    git("config", "user.name", "Source Test")
    git("config", "user.email", "source@example.invalid")
    (root / "src/sova/adapters/sql").mkdir(parents=True)
    (root / "src/sova/__init__.py").write_text("# source\n")
    (root / "src/sova/adapters/sql/schema.sql").write_text("SELECT 1;\n")
    git("add", ".")
    git("commit", "-m", "initial")
    return root, git("rev-parse", "HEAD")


def test_source_inventory_is_fixed_to_committed_python_and_sql(checkout):
    files = image.source_inventory(*checkout, "sova")
    assert files == {"__init__.py": sha("# source\n"), "adapters/sql/schema.sql": sha("SELECT 1;\n")}


@pytest.mark.parametrize("case", ["modified", "untracked", "missing", "symlink"])
def test_working_tree_drift_and_symlinks_are_refused(checkout, case):
    root, revision = checkout
    path = root / "src/sova/__init__.py"
    if case == "modified":
        path.write_text("# changed\n")
    elif case == "untracked":
        (path.parent / "untracked.py").write_text("# extra\n")
    else:
        path.unlink()
        if case == "symlink":
            path.symlink_to(root / "elsewhere.py")
    with pytest.raises(image.ImageCheckError, match="working_source_differs|symlink"):
        image.source_inventory(root, revision, "sova")


@pytest.mark.parametrize("reference", ["HEAD", "main", "abc", "0" * 40 + ":src"])
def test_source_requires_full_revision(checkout, reference):
    with pytest.raises(image.ImageCheckError, match="source_reference_invalid"):
        image.source_inventory(checkout[0], reference, "sova")


@pytest.mark.parametrize("reference", ["sova:latest", "sova@sha256:" + "a" * 64, "sha256:abc"])
def test_image_must_be_full_local_image_id_before_any_docker_call(monkeypatch, reference):
    def forbidden(*args, **kwargs):
        pytest.fail("Mutable image reached Docker")
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    with pytest.raises(image.ImageCheckError, match="immutable_image"):
        image.inspect_image(reference, "arm64")


@pytest.mark.parametrize("field,value", [("Id", "sha256:" + "b" * 64), ("Architecture", "amd64"),
                                        ("Os", "windows"), ("User", "root"),
                                        ("Entrypoint", ["other"])])
def test_runtime_identity_architecture_user_and_entrypoint_are_required(monkeypatch, field, value):
    data = {"Id": "sha256:" + "a" * 64, "Architecture": "arm64", "Os": "linux",
            "Config": {"User": "10001:10001", "Entrypoint": ["sova-service"]}}
    (data["Config"] if field in {"User", "Entrypoint"} else data)[field] = value
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: json.dumps([data]).encode())
    with pytest.raises(image.ImageCheckError):
        image.inspect_image("sha256:" + "a" * 64, "arm64")


def test_probe_is_networkless_unprivileged_and_cleans_only_owned_container(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "run":
            kwargs["stdout"].write(b"{}")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 0, "someone-else\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert image.probe("sha256:" + "a" * 64) == {}
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "10001:10001"
    assert command[command.index("--pull") + 1] == "never"
    assert "--read-only" in command and "--cap-drop" in command
    assert not {"--volume", "--mount", "--publish", "--env-file", "--privileged"} & set(command)
    assert not any(c[1] == "rm" for c in commands)


def test_evidence_is_exclusive_private_and_never_authorizes_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(image, "inspect_image", lambda *a: (_ for _ in ()).throw(image.ImageCheckError("cached_image_unavailable")))
    output = tmp_path / "evidence.json"
    args = ["--image", "sha256:" + "a" * 64, "--architecture", "arm64", "--source", str(tmp_path),
            "--source-revision", "a" * 40, "--security-source", str(tmp_path),
            "--security-revision", "b" * 40, "--output", str(output)]
    assert image.main(args) == 1
    data = json.loads(output.read_text())
    assert data["status"] == "ERROR" and data["error"] == "cached_image_unavailable"
    assert data["qualification_issued"] is data["authorizes_execution"] is data["deployment_observed"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        image.main(args)


@pytest.mark.parametrize("code,output,expected", [
    (1, json.dumps({"level": "ERROR", "msg": "run-worker failed", "exc": "sova.ContractError: refused"}), True),
    (0, json.dumps({"level": "ERROR", "msg": "run-worker failed", "exc": "sova.ContractError: refused"}), False),
    (1, json.dumps({"level": "ERROR", "msg": "run-worker failed", "exc": "ImportError: refused"}), False),
    (1, "refused", False),
    (1, json.dumps({"level": "ERROR", "msg": "run-worker failed", "exc": "ContractError: another refusal"}), False),
])
def test_cli_requires_specific_contract_refusal_not_just_nonzero_exit(code, output, expected):
    from qualification_image_cli import accepted_rejection
    assert accepted_rejection(code, output, "refused") is expected
