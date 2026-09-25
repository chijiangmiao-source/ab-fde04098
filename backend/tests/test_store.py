"""Unit tests for shared-reference retention and two-phase GC semantics."""

from __future__ import annotations

import hashlib

import pytest

from app.store import (
    CANDIDATE_SUFFIX,
    DeletingObjectError,
    EmptyChangeError,
    RevisionConflict,
    Store,
    UnknownObjectError,
)


def make_block(text: str) -> tuple[str, bytes]:
    data = text.encode()
    return hashlib.sha256(data).hexdigest(), data


@pytest.fixture()
def store(tmp_path) -> Store:
    s = Store(str(tmp_path / "data"))
    s.initialize()
    return s


def seed(store: Store, *texts: str) -> list[str]:
    return [store.put_block(t.encode()) for t in texts]


# ------------------------------------------------- shared reference retention
def test_shared_block_retained_when_one_package_unpublished(store):
    shared, only_a, only_b = seed(store, "shared calibration frame", "a-only", "b-only")

    store.submit("pkg-a", 0, [{"op": "add", "sha": shared, "name": "frame-1"}])
    rev = 1
    res = store.submit("pkg-b", rev, [{"op": "add", "sha": shared}, {"op": "add", "sha": only_b}])
    rev = res["rev"]
    store.submit("pkg-a", rev, [{"op": "add", "sha": only_a}], note="a grows")
    rev = store.stats()["rev"]

    assert store.read_block(shared) == b"shared calibration frame"
    assert store.stats()["refcount"][shared] == 2

    # Reviewer takes package A offline: all A-only refs disappear, shared stays.
    pkg_a = store.get_directory("pkg-a")
    res = store.submit(
        "pkg-a", rev, [{"op": "remove", "sha": e["sha"]} for e in pkg_a["entries"]]
    )
    assert res["summary"]["removed"] == 2

    gc = store.gc()  # mark then sweep
    assert only_a in gc["removed"]
    assert shared not in gc["removed"]

    # The other package can still read the shared object through the API path.
    assert store.read_block(shared) == b"shared calibration frame"
    status = store.block_status(shared)
    assert status["state"] == "retained"
    assert status["refcount"] == 1
    assert status["referrers"] == ["pkg-b"]
    assert "pkg-b" in status["reason"]

    # After the LAST reference goes, GC reclaims: unreadable, gone from
    # directory edges and reference counts alike.
    rev = store.stats()["rev"]
    store.submit("pkg-b", rev, [{"op": "remove", "sha": shared}, {"op": "remove", "sha": only_b}])
    gc = store.gc()
    assert set(gc["removed"]) == {shared, only_b}

    with pytest.raises(UnknownObjectError):
        store.read_block(shared)
    status = store.block_status(shared)
    assert status["readable"] is False and status["state"] == "missing"
    assert shared not in store.stats()["refcount"]
    assert store.get_directory("pkg-b")["entries"] == []
    assert store.integrity().ok


def test_only_zero_reference_blocks_are_reclaimable(store):
    sha = seed(store, "kept")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": sha}])
    marked = store.gc_mark()["marked"]
    assert sha not in marked
    assert not store.is_candidate(sha)
    assert store.read_block(sha) == b"kept"


def test_publish_cannot_wire_in_deleting_object(store):
    orphan = seed(store, "orphan bytes")[0]
    assert orphan in store.gc_mark()["marked"]

    with pytest.raises(DeletingObjectError):
        store.submit("late-publisher", 0, [{"op": "add", "sha": orphan}])

    # Nothing changed: directory was never created with the edge.
    assert store.get_directory("late-publisher") is None
    assert orphan not in store.stats()["refcount"]
    # GC settles, a fresh re-upload of identical content afterwards succeeds.
    assert orphan in store.gc_sweep()["removed"]
    with pytest.raises(UnknownObjectError):
        store.read_block(orphan)
    re_uploaded = store.put_block(b"orphan bytes")
    assert re_uploaded == orphan and store.read_block(orphan) == b"orphan bytes"


# ---------------------------------------------------- revision conflict stability
def test_stale_revision_is_stably_rejected_without_mutation(store):
    a = seed(store, "a")[0]
    b = seed(store, "b")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": a}])

    before = store.stats()
    with pytest.raises(RevisionConflict):
        store.submit("pkg-a", 0, [{"op": "add", "sha": b}])  # stale base 0
    after = store.stats()

    assert before == after
    pkg = store.get_directory("pkg-a")
    assert [e["sha"] for e in pkg["entries"]] == [a]
    assert store.stats()["refcount"] == {a: 1}

    # Retrying against the current revision succeeds - stable, not poisoned.
    res = store.submit("pkg-a", 1, [{"op": "add", "sha": b}])
    assert res["rev"] == 2


def test_stale_rejection_leaves_objects_and_gc_state_untouched(store):
    sha = seed(store, "x")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": sha}])
    orphan = seed(store, "orphan")[0]
    store.gc_mark()
    assert store.is_candidate(orphan)

    with pytest.raises(RevisionConflict):
        store.submit("pkg-a", 0, [{"op": "remove", "sha": sha}])

    assert store.read_block(sha) == b"x"
    assert store.is_candidate(orphan)  # GC phase state untouched


def test_empty_submission_rejected(store):
    sha = seed(store, "x")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": sha}])
    with pytest.raises(EmptyChangeError):
        store.submit("pkg-a", 1, [{"op": "add", "sha": sha}])  # duplicate no-op


# ------------------------------------------------------- crash-restart recovery
def test_recover_finishes_torn_removal_with_object_gone(store):
    sha = seed(store, "doomed")[0]
    store.crash_simulate_torn_removal(sha)  # object unlinked, marker left
    assert not store.object_exists(sha) and store.is_candidate(sha)

    # Restart == brand new Store instance over the same directory.
    restarted = Store(store.root)
    report = restarted.recover()
    assert sha in report["removed_candidates"]
    assert not restarted.is_candidate(sha)
    with pytest.raises(UnknownObjectError):
        restarted.read_block(sha)
    assert restarted.integrity().ok


def test_recover_finishes_interrupted_mark_then_remove(store):
    sha = seed(store, "doomed-too")[0]
    # Crash after marker written but before the object unlink.
    store.gc_mark()
    assert store.object_exists(sha) and store.is_candidate(sha)

    restarted = Store(store.root)
    report = restarted.recover()
    assert sha in report["removed_candidates"]
    with pytest.raises(UnknownObjectError):
        restarted.read_block(sha)


def test_recover_releases_stale_marker_on_referenced_object(store):
    sha = seed(store, "still-wanted")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": sha}])
    store.gc_mark()  # marks nothing because refcount is 1

    # Forcibly emulate a marker that must never be honoured while referenced.
    marker = store._candidate_path(sha)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write('{"sha": "%s"}' % sha)
    assert store.is_candidate(sha)

    report = Store(store.root).recover()
    assert sha in report["released_candidates"]
    assert store.read_block(sha) == b"still-wanted"
    assert not store.is_candidate(sha)
    assert store.integrity().ok


def test_recovery_runs_automatically_on_initialize(tmp_path):
    s1 = Store(str(tmp_path / "data"))
    s1.initialize()
    sha = s1.put_block(b"orphan block")
    s1.crash_simulate_torn_removal(sha)

    s2 = Store(str(tmp_path / "data"))
    s2.initialize()  # boot-time convergence
    assert not s2.is_candidate(sha)
    with pytest.raises(UnknownObjectError):
        s2.read_block(sha)


def test_interrupted_mark_sweep_sequence_converges_across_restarts(store):
    shared, a_only, b_only = seed(store, "shared", "a", "b")
    store.submit("pkg-a", 0, [{"op": "add", "sha": shared}, {"op": "add", "sha": a_only}])
    store.submit("pkg-b", 1, [{"op": "add", "sha": shared}, {"op": "add", "sha": b_only}])

    # Tear down A, crash AFTER mark but BEFORE sweep.
    store.submit("pkg-a", 2, [{"op": "remove", "sha": shared}, {"op": "remove", "sha": a_only}])
    marked = store.gc_mark()["marked"]
    assert marked == [a_only]

    Store(store.root).initialize()  # restart converges
    assert store.read_block(shared) == b"shared"
    with pytest.raises(UnknownObjectError):
        store.read_block(a_only)
    assert store.integrity().ok
    assert store.get_directory("pkg-b")["entries"]  # live package intact


def test_revisions_journal_survives_and_reports_summary(store):
    sha = seed(store, "hello")[0]
    store.submit("pkg-a", 0, [{"op": "add", "sha": sha}], note="first")
    revs = store.list_revisions()["revisions"]
    assert [r["rev"] for r in revs] == [1]
    assert revs[0]["summary"] == {"added": 1, "removed": 0}
    assert revs[0]["note"] == "first"


def test_candidate_marker_files_are_json(tmp_path):
    s = Store(str(tmp_path / "d"))
    s.initialize()
    sha = s.put_block(b"tmp")
    s.gc_mark()
    import json
    import os

    marker = os.path.join(s.objects_dir, sha + CANDIDATE_SUFFIX)
    payload = json.load(open(marker, encoding="utf-8"))
    assert payload["sha"] == sha and payload["reason"] == "zero references"
