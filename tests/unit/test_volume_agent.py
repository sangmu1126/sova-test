"""The volume agent and the adapter's difference copy, without a cluster.

Two agents run here as the Pods would run them (the same source text, through
``python3 -I -c``), on temporary directories: a reader over the "source" and a
writer over the "target". The adapter's copy talks to them through a stand-in
for the API server's Pod proxy that only forwards bytes.
"""

import hashlib
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes_volume_handoff import (
    AGENT_SOURCE,
    KubernetesVolumeHandoffAdapter,
    _Agent,
)
from sova.domain.contracts import ContractError

TOKEN = "t" * 64
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def agents(tmp_path):
    started = {}

    def start(name, mode, claims=1):
        root = tmp_path / name
        for i in range(claims):
            (root / str(i)).mkdir(parents=True)
        port = free_port()
        env = {"SOVA_AGENT_ROOT": str(root), "SOVA_AGENT_MODE": mode, "SOVA_AGENT_PORT": str(port),
               "SOVA_AGENT_CLAIMS": str(claims),
               "SOVA_AGENT_TOKEN_SHA256": hashlib.sha256(TOKEN.encode()).hexdigest(),
               "PATH": os.environ["PATH"]}
        process = subprocess.Popen([sys.executable, "-I", "-c", AGENT_SOURCE], env=env)
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        started[name] = (process, port)
        return root / "0", port

    yield start
    for process, _ in started.values():
        process.terminate()
        process.wait(5)


class Proxy:
    """Stands in for the API server's Pod proxy: forwards to a local agent."""

    def __init__(self, ports):
        self.ports = ports

    def proxy(self, method, pod, port, path, query, *, headers, body=None, timeout=60,
              limit=1 << 20):
        from urllib.parse import urlencode
        connection = http.client.HTTPConnection("127.0.0.1", self.ports[pod], timeout=timeout)
        connection.request(method, path + "?" + urlencode(query), body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        if response.status != 200:
            raise KubernetesRequestRefused(response.status)
        return data


def agent(ports, name, token=TOKEN):
    return _Agent(Proxy(ports), name, token)


def raw(port, method, path, token=TOKEN, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body,
                       headers={"X-Sova-Agent-Token": token} if token else {})
    response = connection.getresponse()
    return response.status, response.read()


def adapter(chunk=64 << 10):
    self = object.__new__(KubernetesVolumeHandoffAdapter)
    self.sides = {"source": SimpleNamespace(claims=("data",)),
                  "target": SimpleNamespace(claims=("data",))}
    self.chunk_bytes = chunk
    return self


def build_source(root):
    (root / "board.db").write_bytes(os.urandom(200_000))  # several chunks
    (root / "uploads" / "2026" / "11").mkdir(parents=True)
    for i in range(5):
        (root / "uploads" / f"{i}.txt").write_text(f"attachment {i}\n")
    (root / "uploads" / "2026" / "11" / "big.bin").write_bytes(os.urandom(150_000))
    (root / "empty.txt").write_bytes(b"")
    (root / "cache").mkdir()
    (root / "private.key").write_text("not really")
    (root / "private.key").chmod(0o640)
    (root / "frozen").mkdir()
    (root / "frozen" / "inside.txt").write_text("kept")
    (root / "frozen").chmod(0o555)
    (root / "latest").symlink_to("uploads/4.txt")


def test_only_healthz_is_open_and_a_reader_refuses_every_change(agents):
    root, port = agents("source", "reader")
    (root / "a.txt").write_text("a")
    assert raw(port, "GET", "/healthz", token=None)[0] == 200
    assert raw(port, "GET", "/manifest?claim=0", token=None)[0] == 403
    assert raw(port, "GET", "/manifest?claim=0", token="u" * 64)[0] == 403
    status, body = raw(port, "GET", "/manifest?claim=0")
    assert status == 200 and json.loads(body)["entries"][0][0] == "a.txt"
    assert raw(port, "PUT", "/write?claim=0&path=.sova-partial-x&offset=0", body=b"x")[0] == 405
    assert raw(port, "POST", "/remove?claim=0&path=a.txt", body=b"")[0] == 405
    assert (root / "a.txt").exists()


def test_paths_never_leave_the_volume(agents, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host file")
    root, port = agents("target", "writer")
    (root / "escape").symlink_to(outside)
    (root / "file-link").symlink_to(outside / "secret.txt")
    status, body = raw(port, "GET", "/manifest?claim=0")
    entries = json.loads(body)["entries"]
    assert [e[:2] for e in entries] == [["escape", "l"], ["file-link", "l"]]  # not followed
    for path in ("../outside/secret.txt", "/etc/passwd", "a//b", "./x"):
        assert raw(port, "GET", f"/read?claim=0&path={path}&offset=0&length=10")[0] == 400
    assert raw(port, "GET", "/read?claim=0&path=escape/secret.txt&offset=0&length=10")[0] == 409
    assert raw(port, "GET", "/read?claim=0&path=file-link&offset=0&length=10")[0] == 409
    assert raw(port, "PUT", "/write?claim=0&path=escape/.sova-partial-x&offset=0",
               body=b"x")[0] == 409
    # Content arrives only through a checked commit, never straight to a name.
    assert raw(port, "PUT", "/write?claim=0&path=board.db&offset=0", body=b"x")[0] == 400
    assert raw(port, "GET", "/manifest?claim=1")[0] == 400
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_a_commit_with_other_content_is_refused_and_leaves_nothing(agents):
    root, port = agents("target", "writer")
    assert raw(port, "PUT", "/write?claim=0&path=.sova-partial-a&offset=0", body=b"hello")[0] == 200
    wrong = hashlib.sha256(b"other").hexdigest()
    assert raw(port, "POST", f"/commit?claim=0&partial=.sova-partial-a&path=a.txt&size=5"
                             f"&sha256={wrong}&mode=420", body=b"")[0] == 409
    assert os.listdir(root) == []
    good = hashlib.sha256(b"hello").hexdigest()
    raw(port, "PUT", "/write?claim=0&path=.sova-partial-a&offset=0", body=b"hello")
    assert raw(port, "POST", f"/commit?claim=0&partial=.sova-partial-a&path=a.txt&size=5"
                             f"&sha256={good}&mode=416", body=b"")[0] == 200
    assert (root / "a.txt").read_bytes() == b"hello"
    assert stat.S_IMODE((root / "a.txt").stat().st_mode) == 0o640


def test_special_files_are_reported_not_copied(agents):
    root, port = agents("source", "reader")
    os.mkfifo(root / "pipe")
    body = json.loads(raw(port, "GET", "/manifest?claim=0")[1])
    assert body["entries"] == [] and body["unsupported"] == [["pipe", "special_file"]]


def test_the_copy_makes_the_target_tree_identical(agents):
    source_root, source_port = agents("source", "reader")
    target_root, target_port = agents("target", "writer")
    build_source(source_root)
    # The target holds stale and conflicting entries from an earlier copy.
    (target_root / "uploads").mkdir()
    (target_root / "uploads" / "0.txt").write_text("stale")
    (target_root / "uploads" / "gone.txt").write_text("removed on the source")
    (target_root / "cache").write_text("a file where the source has a directory")
    (target_root / "empty.txt").mkdir()  # a directory where the source has a file
    (target_root / "private.key").write_text("not really")  # same content, other mode
    (target_root / "latest").symlink_to("uploads/0.txt")
    ports = {"source": source_port, "target": target_port}
    source, target = agent(ports, "source"), agent(ports, "target")
    report = KubernetesVolumeHandoffAdapter._sync(adapter(), source, target, live=False)
    want, have = source.manifest(0), target.manifest(0)
    assert have["entries"] == want["entries"], [
        (a, b) for a, b in zip(want["entries"], have["entries"]) if a != b]
    assert report["removed"] >= 3 and report["copied_files"] >= 8
    # A second copy finds nothing to do.
    again = KubernetesVolumeHandoffAdapter._sync(adapter(), source, target, live=False)
    assert again == {"copied_files": 0, "copied_bytes": 0, "removed": 0, "changed_later": 0}


class Changing(_Agent):
    """A source whose file changes between the listing and the copy."""

    def read(self, claim, path, offset, length):
        data = super().read(claim, path, offset, length)
        return data[:-1] + bytes([data[-1] ^ 1]) if path == "board.db" else data


def test_a_source_that_changes_fails_the_final_copy_and_waits_for_it_while_live(agents):
    source_root, source_port = agents("source", "reader")
    target_root, target_port = agents("target", "writer")
    build_source(source_root)
    ports = {"source": source_port, "target": target_port}
    changing, target = Changing(Proxy(ports), "source", TOKEN), agent(ports, "target")
    with pytest.raises(ContractError, match="changed during the copy"):
        KubernetesVolumeHandoffAdapter._sync(adapter(), changing, target, live=False)
    assert not (target_root / "board.db").exists()
    report = KubernetesVolumeHandoffAdapter._sync(adapter(), changing, target, live=True)
    assert report["changed_later"] == 1 and not (target_root / "board.db").exists()
    assert not [n for n in os.listdir(target_root) if n.startswith(".sova-partial-")]
