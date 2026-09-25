"""HTTP API tests: submission, revision conflicts, GC, health, retention."""

from __future__ import annotations

import hashlib
import json

import pytest

from app.api import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(data_dir=str(tmp_path / "data"), static_dir=str(tmp_path / "none"))
    app.testing = True
    return app.test_client()


def upload(client, text: str) -> str:
    rv = client.post("/api/blocks", data=text.encode())
    assert rv.status_code == 201, rv.data
    return rv.get_json()["sha"]


def submit(client, directory, base_rev, changes, expect=201):
    rv = client.post(
        "/api/submit",
        json={"directory": directory, "base_rev": base_rev, "changes": changes},
    )
    assert rv.status_code == expect, rv.data
    return rv


def test_block_roundtrip_and_404_after_last_ref_gc(client):
    sha = upload(client, "frame bytes")
    assert client.get(f"/api/blocks/{sha}").data == b"frame bytes"

    submit(client, "pkg-a", 0, [{"op": "add", "sha": sha}])
    assert client.post("/api/gc", json={"mode": "auto"}).get_json()["removed"] == []
    assert client.get(f"/api/blocks/{sha}").status_code == 200

    submit(client, "pkg-a", 1, [{"op": "remove", "sha": sha}])
    removed = client.post("/api/gc", json={"mode": "auto"}).get_json()["removed"]
    assert removed == [sha]
    assert client.get(f"/api/blocks/{sha}").status_code == 404
    # Directory edges and refcounts no longer carry it.
    state = client.get("/api/state").get_json()
    assert sha not in state["stats"]["refcount"]
    assert state["stats"]["objects"] == 0


def test_stale_revision_conflict_is_stable(client):
    a = upload(client, "a")
    b = upload(client, "b")
    submit(client, "pkg-a", 0, [{"op": "add", "sha": a}])

    rv = submit(client, "pkg-a", 0, [{"op": "add", "sha": b}], expect=409)
    body = rv.get_json()
    assert body["code"] == "revision_conflict"
    assert body["current_rev"] == 1

    # Existing directory, refcounts and objects are untouched.
    pkg = client.get("/api/directories/pkg-a").get_json()
    assert [e["sha"] for e in pkg["entries"]] == [a]
    assert client.get("/api/stats").get_json()["refcount"] == {a: 1}
    assert client.get(f"/api/blocks/{b}").status_code == 200

    # Retrying on the current revision succeeds.
    submit(client, "pkg-a", 1, [{"op": "add", "sha": b}])
    assert client.get("/api/stats").get_json()["rev"] == 2


def test_publish_unknown_object_rejected(client):
    ghost = hashlib.sha256(b"never uploaded").hexdigest()
    rv = submit(client, "pkg-a", 0, [{"op": "add", "sha": ghost}], expect=422)
    assert rv.get_json()["code"] == "unknown_object"
    assert client.get("/api/directories").get_json()["directories"] == []


def test_publish_deleting_object_rejected_and_settles(client):
    orphan = upload(client, "orphan")
    assert client.post("/api/gc", json={"mode": "mark"}).get_json()["marked"] == [orphan]

    rv = submit(client, "late", 0, [{"op": "add", "sha": orphan}], expect=409)
    assert rv.get_json()["code"] == "deleting_object"
    assert client.get("/api/directories").get_json()["directories"] == []

    # Marked object is not readable through the API either.
    assert client.get(f"/api/blocks/{orphan}").status_code == 404

    swept = client.post("/api/gc", json={"mode": "sweep"}).get_json()
    assert swept["removed"] == [orphan]


def test_shared_reference_retention_story(client):
    """The headline scenario, entirely over HTTP."""
    shared = upload(client, "shared dark-frame block")
    a_only = upload(client, "a calibration")
    b_only = upload(client, "b calibration")

    submit(client, "pkg-a", 0, [{"op": "add", "sha": shared, "name": "dark"},
                                {"op": "add", "sha": a_only, "name": "a"}])
    submit(client, "pkg-b", 1, [{"op": "add", "sha": shared, "name": "dark"},
                                {"op": "add", "sha": b_only, "name": "b"}])

    # Reviewer unpublishes package A and runs cleanup.
    pkg_a = client.get("/api/directories/pkg-a").get_json()
    submit(client, "pkg-a", 2, [{"op": "remove", "sha": e["sha"]} for e in pkg_a["entries"]])
    gc = client.post("/api/gc", json={"mode": "auto"}).get_json()
    assert gc["removed"] == [a_only]

    # Package B keeps reading the shared block; the status endpoint explains why.
    assert client.get(f"/api/blocks/{shared}").data == b"shared dark-frame block"
    status = client.get(f"/api/blocks/{shared}/status").get_json()
    assert status["readable"] is True
    assert status["state"] == "retained"
    assert status["referrers"] == ["pkg-b"]
    assert status["refcount"] == 1

    # Last reference removed -> unreadable and forgotten by stats.
    pkg_b = client.get("/api/directories/pkg-b").get_json()
    submit(client, "pkg-b", 3, [{"op": "remove", "sha": e["sha"]} for e in pkg_b["entries"]])
    client.post("/api/gc", json={"mode": "auto"})
    assert client.get(f"/api/blocks/{shared}").status_code == 404
    final_status = client.get(f"/api/blocks/{shared}/status").get_json()
    assert final_status["state"] == "missing"
    assert final_status["refcount"] == 0
    stats = client.get("/api/stats").get_json()
    assert shared not in stats["refcount"] and stats["objects"] == 0
    assert client.get("/health").get_json()["integrity"]["ok"] is True


def test_two_phase_gc_endpoints(client):
    sha = upload(client, "temp")
    mark = client.post("/api/gc", json={"mode": "mark"}).get_json()
    assert mark["marked"] == [sha]
    # Between mark and sweep: candidate listed, object still on disk.
    stats = client.get("/api/stats").get_json()
    assert stats["candidates"] == [sha] and stats["objects"] == 1
    assert client.get(f"/api/blocks/{sha}").status_code == 404  # not servable while marked
    sweep = client.post("/api/gc", json={"mode": "sweep"}).get_json()
    assert sweep["removed"] == [sha]
    assert client.get("/api/stats").get_json()["objects"] == 0


def test_revisions_and_summaries_listed(client):
    sha = upload(client, "r")
    submit(client, "pkg-a", 0, [{"op": "add", "sha": sha}])
    submit(client, "pkg-a", 1, [{"op": "remove", "sha": sha}], )
    revs = client.get("/api/revisions").get_json()["revisions"]
    assert [r["rev"] for r in revs] == [1, 2]
    assert revs[0]["summary"] == {"added": 1, "removed": 0}
    assert revs[1]["summary"] == {"added": 0, "removed": 1}
    assert revs[1]["parent_rev"] == 1


def test_health_reports_writability_and_integrity(client):
    body = client.get("/health").get_json()
    assert body["status"] == "ok"
    assert body["objects_dir_writable"] is True
    assert body["integrity"]["ok"] is True
    assert client.get("/health").status_code == 200


def test_health_503_when_objects_dir_readonly(client, tmp_path):
    objects = tmp_path / "data" / "objects"
    objects.chmod(0o555)
    try:
        rv = client.get("/health")
        assert rv.status_code == 503
        assert rv.get_json()["objects_dir_writable"] is False
    finally:
        objects.chmod(0o755)


def test_unpublish_convenience_endpoint(client):
    s1 = upload(client, "x")
    s2 = upload(client, "y")
    submit(client, "pkg-a", 0, [{"op": "add", "sha": s1}, {"op": "add", "sha": s2}])
    rv = client.post("/api/packages/pkg-a/unpublish", json={"base_rev": 1})
    assert rv.status_code == 201
    assert rv.get_json()["summary"]["removed"] == 2
    client.post("/api/gc", json={"mode": "auto"})
    assert client.get("/api/directories/pkg-a").get_json()["entries"] == []
    assert client.get("/api/stats").get_json()["objects"] == 0


def test_root_serves_index_when_present(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>ok")
    app = create_app(data_dir=str(tmp_path / "data"), static_dir=str(static))
    rv = app.test_client().get("/")
    assert rv.status_code == 200 and b"ok" in rv.data
