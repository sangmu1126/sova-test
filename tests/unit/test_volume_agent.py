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

import pytest

from sova.adapters.kubernetes.https import KubernetesRequestRefused
from sova.adapters.kubernetes.volume_agents import (
    AGENT_SOURCE,
    Agent,
    differences,
    manifest_digest,
    same_tree,
    subtree_links,
    sync_trees,
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
        scratch = tmp_path / f"{name}-scratch"
        scratch.mkdir()
        env = {"SOVA_AGENT_ROOT": str(root), "SOVA_AGENT_MODE": mode, "SOVA_AGENT_PORT": str(port),
               "SOVA_AGENT_SCRATCH": str(scratch),
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
    return Agent(Proxy(ports), name, token)


def raw(port, method, path, token=TOKEN, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body,
                       headers={"X-Sova-Agent-Token": token} if token else {})
    response = connection.getresponse()
    return response.status, response.read()


def sync(source, target, *, live):
    return sync_trees(source, target, 1, chunk=64 << 10, live=live)


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
    report = sync(source, target, live=False)
    want, have = source.manifest(0), target.manifest(0)
    assert have["entries"] == want["entries"], [
        (a, b) for a, b in zip(want["entries"], have["entries"]) if a != b]
    assert report["removed"] >= 3 and report["copied_files"] >= 8
    # A second copy finds nothing to do.
    again = sync(source, target, live=False)
    assert again == {"copied_files": 0, "copied_bytes": 0, "removed": 0, "linked": 0,
                     "changed_later": 0}


class Changing(Agent):
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
        sync(changing, target, live=False)
    assert not (target_root / "board.db").exists()
    report = sync(changing, target, live=True)
    assert report["changed_later"] == 1 and not (target_root / "board.db").exists()
    assert not [n for n in os.listdir(target_root) if n.startswith(".sova-partial-")]


def test_a_sqlite_database_is_read_from_a_copy_never_from_the_volume(agents):
    import sqlite3

    root, port = agents("source", "reader")
    db = sqlite3.connect(root / "board.db", isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, "
               "attachment TEXT)")
    db.execute("INSERT INTO posts(title, attachment) VALUES ('a', '1.txt'), ('b', NULL)")
    # The writer is still open: the last rows are only in the log (-wal).
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert "board.db-wal" in before
    proxy = Agent(Proxy({"source": port}), "source", TOKEN)
    report = proxy.sqlite(0, "board.db", {"files": "SELECT attachment FROM posts "
                                                   "WHERE attachment IS NOT NULL",
                                          "change": "DELETE FROM posts"})
    assert report["integrity"] == ["ok"]
    assert report["tables"]["posts"]["rows"] == 2
    assert report["results"]["files"]["rows"] == [["1.txt"]]
    assert "error" in report["results"]["change"]  # queries are read-only
    after = {p.name: p.read_bytes() for p in root.iterdir()}
    assert after == before  # SQLite never opened the volume's files
    db.close()
    status, _ = raw(port, "POST", "/sqlite?claim=0&path=missing.db", body=b"{}")
    assert status == 409


# -- hard links -----------------------------------------------------------------


def inode(path):
    return os.stat(path, follow_symlinks=False).st_ino


def build_linked(root):
    """A git-like object store and a Maildir delivery: files named twice or more."""
    (root / "objects" / "ab").mkdir(parents=True)
    (root / "objects" / "ab" / "cdef").write_bytes(os.urandom(100_000))
    (root / "clone" / "objects" / "ab").mkdir(parents=True)
    os.link(root / "objects" / "ab" / "cdef", root / "clone" / "objects" / "ab" / "cdef")
    os.link(root / "objects" / "ab" / "cdef", root / "pack")
    (root / "Maildir" / "new").mkdir(parents=True)
    (root / "Maildir" / "tmp").mkdir()
    (root / "Maildir" / "tmp" / "1.msg").write_text("Subject: hi\n")
    os.link(root / "Maildir" / "tmp" / "1.msg", root / "Maildir" / "new" / "1.msg")
    (root / "alone.txt").write_text("one name")


def test_hard_links_are_listed_as_groups_of_paths(agents):
    root, port = agents("source", "reader")
    build_linked(root)
    body = json.loads(raw(port, "GET", "/manifest?claim=0")[1])
    assert body["links"] == [["Maildir/new/1.msg", "Maildir/tmp/1.msg"],
                             ["clone/objects/ab/cdef", "objects/ab/cdef", "pack"]]
    assert body["unsupported"] == []
    files = {e[0]: e for e in body["entries"] if e[1] == "f"}
    assert files["pack"][5:] == files["objects/ab/cdef"][5:]  # each name listed in full


def test_a_hard_link_to_a_symbolic_link_is_not_supported(agents):
    root, port = agents("source", "reader")
    (root / "to").symlink_to("somewhere")
    try:
        os.link(root / "to", root / "also", follow_symlinks=False)
    except (NotImplementedError, OSError):
        pytest.skip("this file system cannot link a symbolic link")
    body = json.loads(raw(port, "GET", "/manifest?claim=0")[1])
    assert sorted(body["unsupported"]) == [["also", "hard_linked_symlink"],
                                           ["to", "hard_linked_symlink"]]


def test_the_copy_keeps_hard_links_and_breaks_links_the_source_does_not_have(agents):
    source_root, source_port = agents("source", "reader")
    target_root, target_port = agents("target", "writer")
    build_linked(source_root)
    (source_root / "x.txt").write_text("same")
    (source_root / "y.txt").write_text("same")
    # The target: x and y linked (the source has two files), the store half
    # linked, a directory where the source has a linked name.
    (target_root / "x.txt").write_text("same")
    os.link(target_root / "x.txt", target_root / "y.txt")
    (target_root / "objects" / "ab").mkdir(parents=True)
    (target_root / "objects" / "ab" / "cdef").write_bytes(
        (source_root / "objects" / "ab" / "cdef").read_bytes())
    os.link(target_root / "objects" / "ab" / "cdef", target_root / "pack")
    (target_root / "Maildir" / "new" / "1.msg").mkdir(parents=True)
    ports = {"source": source_port, "target": target_port}
    source, target = agent(ports, "source"), agent(ports, "target")
    report = sync(source, target, live=False)
    want, have = source.manifest(0), target.manifest(0)
    assert same_tree(want, have), differences(want["entries"], have["entries"],
                                              source_links=want["links"],
                                              target_links=have["links"])
    # Measured apart from the manifest: the names share one file each, or none.
    store = {inode(target_root / p) for p in ("objects/ab/cdef", "pack",
                                               "clone/objects/ab/cdef")}
    mail = {inode(target_root / "Maildir" / d / "1.msg") for d in ("new", "tmp")}
    assert len(store) == 1 and len(mail) == 1
    assert inode(target_root / "x.txt") != inode(target_root / "y.txt")
    assert (target_root / "clone" / "objects" / "ab" / "cdef").stat().st_nlink == 3
    assert report["linked"] >= 2
    assert not [n for n in os.listdir(target_root) if n.startswith(".sova-")]
    # Nothing left to do.
    again = sync(source, target, live=False)
    assert again == {"copied_files": 0, "copied_bytes": 0, "removed": 0, "linked": 0,
                     "changed_later": 0}


def test_the_comparison_sees_a_lost_link(agents):
    """The same bytes under every name, one link fewer: not the same tree."""
    source_root, source_port = agents("source", "reader")
    target_root, target_port = agents("target", "writer")
    build_linked(source_root)
    ports = {"source": source_port, "target": target_port}
    source, target = agent(ports, "source"), agent(ports, "target")
    sync(source, target, live=False)
    copy = target_root / "pack"
    data = copy.read_bytes()
    copy.unlink()
    copy.write_bytes(data)
    copy.chmod(stat.S_IMODE((source_root / "pack").stat().st_mode))
    want, have = source.manifest(0), target.manifest(0)
    assert want["entries"] == have["entries"] and not same_tree(want, have)
    found = differences(want["entries"], have["entries"], source_links=want["links"],
                        target_links=have["links"])
    assert {"path": "pack", "source_links": ["clone/objects/ab/cdef", "objects/ab/cdef",
                                             "pack"], "target_links": ["pack"]} in found
    assert manifest_digest(want["entries"], want["links"]) != \
        manifest_digest(have["entries"], have["links"])
    # A protected set sees the link from inside it to outside it that was lost.
    assert subtree_links(want["links"], "pack") == [["pack"]]
    assert subtree_links(have["links"], "pack") == []
    assert sync(source, target, live=False)["linked"] == 1
    assert same_tree(source.manifest(0), target.manifest(0))


def test_a_tree_without_links_digests_as_before():
    entries = [["a", "f", 420, 1, 1, 1, "x"]]
    assert manifest_digest(entries, []) == manifest_digest(entries) == hashlib.sha256(
        json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def test_the_link_request_names_only_a_whole_file_in_the_volume(agents):
    root, port = agents("target", "writer")
    (root / "a.txt").write_text("a")
    (root / "d").mkdir()
    (root / "to").symlink_to("a.txt")
    (root / "busy").mkdir()
    (root / "busy" / "inside").write_text("a directory replaced by the link")

    def link(existing, path):
        return raw(port, "POST", f"/link?claim=0&existing={existing}&path={path}", body=b"")[0]

    assert link("missing", "b.txt") == 409
    assert link("d", "b.txt") == 409  # a directory is never linked
    assert link("to", "b.txt") == 409  # nor a symbolic link (it is not followed)
    assert link("../a.txt", "b.txt") == 400
    assert link("a.txt", "../b.txt") == 400
    assert link("a.txt", "b.txt") == 200 and inode(root / "b.txt") == inode(root / "a.txt")
    assert link("a.txt", "b.txt") == 200  # already that file
    assert link("a.txt", "busy") == 200 and inode(root / "busy") == inode(root / "a.txt")
    assert sorted(n for n in os.listdir(root) if n.startswith(".sova-")) == []
    reader_root, reader_port = agents("source", "reader")
    (reader_root / "a.txt").write_text("a")
    assert raw(reader_port, "POST", "/link?claim=0&existing=a.txt&path=b.txt",
               body=b"")[0] == 405
    assert not (reader_root / "b.txt").exists()


class ChangingStore(Agent):
    """A live source whose linked file changes while it is copied."""

    def read(self, claim, path, offset, length):
        data = super().read(claim, path, offset, length)
        return data[:-1] + bytes([data[-1] ^ 1]) if path == "clone/objects/ab/cdef" else data


def test_a_linked_file_that_changes_while_live_leaves_its_whole_group_for_later(agents):
    source_root, source_port = agents("source", "reader")
    target_root, target_port = agents("target", "writer")
    build_linked(source_root)
    ports = {"source": source_port, "target": target_port}
    changing, target = ChangingStore(Proxy(ports), "source", TOKEN), agent(ports, "target")
    report = sync(changing, target, live=True)
    assert report["changed_later"] == 1  # the group's first name; its other names wait
    for name in ("pack", "objects/ab/cdef", "clone/objects/ab/cdef"):
        assert not (target_root / name).exists()
    assert inode(target_root / "Maildir" / "new" / "1.msg") == \
        inode(target_root / "Maildir" / "tmp" / "1.msg")
