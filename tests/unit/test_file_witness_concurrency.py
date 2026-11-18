"""Separate processes must share one file chain and never read a partial append."""

import hashlib
import multiprocessing
import os

import pytest

from sova.adapters.file_authority_witness import FileAuthorityWitness
from sova.domain.contracts import ContractError, canonical_json

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX local-file locking profile")
UNIT = "1" * 64


def append_child(path, ready, release, done, paused):
    import sova.adapters.file_authority_witness as module
    if paused:
        original = module.canonical_json
        def wait_before_write(value):
            line = original(value)
            ready.set()
            if not release.wait(10):
                raise RuntimeError("Test writer was not released")
            return line
        module.canonical_json = wait_before_write
    else:
        ready.set()
    FileAuthorityWitness(path, tenant_id="tenant-a").append(
        "orders" if paused else "other", UNIT, "step", {"revision": 2})
    done.set()


def read_child(path, started, done, result):
    started.set()
    try:
        result.put(("value", FileAuthorityWitness(path, tenant_id="tenant-a").latest("orders", UNIT)))
    except Exception as error:
        result.put(("error", type(error).__name__))
    done.set()


def finish(processes):
    for process in processes:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
            pytest.fail("Run-owned witness test child did not stop")
        assert process.exitcode == 0


def test_two_processes_do_not_fork_the_hash_chain(tmp_path):
    path = tmp_path / "witness"
    FileAuthorityWitness(path, tenant_id="tenant-a").append("orders", UNIT, "open", {"revision": 1})
    ctx = multiprocessing.get_context("spawn")
    ready, release, done, second_ready, second_done = [ctx.Event() for _ in range(5)]
    processes = [ctx.Process(target=append_child, args=(path, ready, release, done, True)),
                 ctx.Process(target=append_child, args=(path, second_ready, release, second_done, False))]
    processes[0].start()
    try:
        assert ready.wait(10)
        processes[1].start()
        assert second_ready.wait(10)
        assert not second_done.wait(.5), "Second writer bypassed the first process's chain lock"
    finally:
        release.set()
        finish([p for p in processes if p.pid is not None])
    witness = FileAuthorityWitness(path, tenant_id="tenant-a")
    assert witness.latest("orders", UNIT) == witness.latest("other", UNIT) == {"revision": 2}
    assert len(path.read_text().splitlines()) == 3


def test_a_reader_waits_for_a_cooperating_process_to_finish_its_line(tmp_path):
    import fcntl
    path = tmp_path / "witness"
    FileAuthorityWitness(path, tenant_id="tenant-a").append("orders", UNIT, "open", {"revision": 1})
    previous = hashlib.sha256(path.read_bytes().rstrip(b"\n")).hexdigest()
    line = canonical_json({"tenant_id": "tenant-a", "application_id": "orders", "unit_digest": UNIT,
                           "action": "step", "record": {"revision": 2}, "prev": previous}) + "\n"
    ctx = multiprocessing.get_context("spawn")
    started, done, result = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=read_child, args=(path, started, done, result))
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(line[:len(line)//2])
        stream.flush()
        process.start()
        try:
            assert started.wait(10)
            assert not done.wait(.5), "Reader consumed an uncommitted partial witness line"
        finally:
            stream.write(line[len(line)//2:])
            stream.flush()
            fcntl.flock(stream, fcntl.LOCK_UN)
    finish([process])
    assert result.get(timeout=2) == ("value", {"revision": 2})
    result.close()
    result.join_thread()


@pytest.mark.parametrize("value", ["{", "[]", "null", '{"prev":"0"}'])
def test_malformed_witness_records_fail_as_contract_errors(tmp_path, value):
    path = tmp_path / "witness"
    path.write_text(value + "\n")
    with pytest.raises(ContractError):
        FileAuthorityWitness(path, tenant_id="tenant-a").latest("orders", UNIT)
