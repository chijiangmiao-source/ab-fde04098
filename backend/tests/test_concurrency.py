"""Concurrency checks for the single exclusive flock boundary."""

from __future__ import annotations

import threading

import pytest

from app.store import RevisionConflict, Store


@pytest.fixture()
def store(tmp_path) -> Store:
    s = Store(str(tmp_path / "data"))
    s.initialize()
    return s


def test_concurrent_submits_serialize_with_one_winner(store):
    shas = [store.put_block(f"block-{i}".encode()) for i in range(10)]
    barrier = threading.Barrier(len(shas))
    results: list[Exception | dict] = []
    lock = threading.Lock()

    def worker(sha: str):
        barrier.wait()
        try:
            res = store.submit("hot-pkg", 0, [{"op": "add", "sha": sha}])
        except Exception as exc:  # noqa: BLE001
            res = exc
        with lock:
            results.append(res)

    threads = [threading.Thread(target=worker, args=(sha,)) for sha in shas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, RevisionConflict)]
    assert len(winners) == 1
    assert len(conflicts) == len(shas) - 1
    assert store.stats()["rev"] == 1

    # Every loser can be applied sequentially against the current revision.
    rev = 1
    for sha in shas:
        if sha == winners[0]["changes"][0]["sha"]:
            continue
        rev = store.submit("hot-pkg", rev, [{"op": "add", "sha": sha}])["rev"]

    pkg = store.get_directory("hot-pkg")
    assert sorted(e["sha"] for e in pkg["entries"]) == sorted(shas)
    assert all(v == 1 for v in store.stats()["refcount"].values())
    assert store.integrity().ok


def test_lock_is_process_wide_flock(tmp_path):
    """A second Store over the same root blocks on the exclusive lock."""
    import fcntl
    import os

    s1 = Store(str(tmp_path / "data"))
    s1.initialize()
    s2 = Store(str(tmp_path / "data"))

    fd = os.open(s1.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with s1._locked():
            # Non-blocking acquire from another "process" must fail immediately.
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    # After release, acquisition succeeds.
    with s2._locked():
        assert True
