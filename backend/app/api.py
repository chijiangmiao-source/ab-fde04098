"""HTTP API for the calibration-block rebuild package service."""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from .store import (
    DeletingObjectError,
    EmptyChangeError,
    RevisionConflict,
    Store,
    UnknownObjectError,
)


def create_app(data_dir: str | None = None, static_dir: str | None = None) -> Flask:
    data_dir = data_dir or os.environ.get("DATA_DIR", "/data")
    static_dir = static_dir or os.environ.get(
        "STATIC_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "static")
    )
    static_dir = os.path.abspath(static_dir)

    store = Store(data_dir)
    store.initialize()

    app = Flask(__name__, static_folder=None)
    app.config["STORE"] = store

    def err(message: str, status: int, **extra: Any) -> tuple[Response, int]:
        payload: dict[str, Any] = {"error": message}
        payload.update(extra)
        return jsonify(payload), status

    # ------------------------------------------------------------- blocks
    @app.post("/api/blocks")
    def put_block() -> tuple[Response, int] | Response:
        content = request.get_data() or b""
        if not content:
            return err("empty block content", 400)
        expected = request.args.get("sha")
        try:
            sha = store.put_block(content, expected)
        except DeletingObjectError as exc:
            return err(str(exc), 409, code="deleting_object")
        return jsonify({"sha": sha, "size": len(content)}), 201

    @app.get("/api/blocks/<sha>")
    def get_block(sha: str) -> Response | tuple[Response, int]:
        try:
            data = store.read_block(sha)
        except UnknownObjectError:
            return err(f"block {sha} is not readable", 404)
        return Response(data, mimetype="text/plain")

    @app.get("/api/blocks/<sha>/status")
    def block_status(sha: str) -> Response | tuple[Response, int]:
        if not Store._valid_sha(sha):
            return err("invalid sha-256", 400)
        return jsonify(store.block_status(sha))

    # -------------------------------------------------------- directories
    @app.get("/api/directories")
    def list_directories() -> Response:
        return jsonify(store.list_directories())

    @app.get("/api/directories/<path:name>")
    def get_directory(name: str) -> Response | tuple[Response, int]:
        pkg = store.get_directory(name)
        if pkg is None:
            return err(f"directory {name!r} not found", 404)
        return jsonify(pkg)

    # ------------------------------------------------------------ submit
    @app.post("/api/submit")
    def submit() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        directory = body.get("directory")
        base_rev = body.get("base_rev")
        changes = body.get("changes")
        note = body.get("note", "")
        if not directory or not isinstance(directory, str):
            return err("directory is required", 400)
        if not isinstance(base_rev, int) or isinstance(base_rev, bool):
            return err("base_rev must be an integer", 400)
        if not isinstance(changes, list) or not changes:
            return err("changes must be a non-empty list", 400)
        try:
            result = store.submit(directory, base_rev, changes, note=note)
        except RevisionConflict as exc:
            return err(str(exc), 409, code="revision_conflict", current_rev=store.stats()["rev"])
        except DeletingObjectError as exc:
            return err(str(exc), 409, code="deleting_object")
        except UnknownObjectError as exc:
            return err(f"unknown object {exc}", 422, code="unknown_object")
        except EmptyChangeError as exc:
            return err(str(exc), 400, code="empty_change")
        except Exception as exc:  # noqa: BLE001 - validation errors carry 4xx
            return err(str(exc), 400)
        return jsonify(result), 201

    # ---------------------------------------------------------------- gc
    @app.post("/api/gc")
    def gc() -> Response | tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "auto")
        if mode not in ("auto", "mark", "sweep"):
            return err("mode must be one of auto/mark/sweep", 400)
        return jsonify(store.gc(mode))

    @app.post("/api/admin/recover")
    def recover() -> Response:
        """Converge interrupted mark/sweep after a restart (also runs at boot)."""
        return jsonify(store.recover())

    # ------------------------------------------------------------- reads
    @app.get("/api/revisions")
    def revisions() -> Response:
        return jsonify(store.list_revisions())

    @app.get("/api/stats")
    def stats() -> Response:
        return jsonify(store.stats())

    @app.post("/api/packages/<path:name>/unpublish")
    def unpublish(name: str) -> Response | tuple[Response, int]:
        """Convenience: remove every entry of one package in one revision."""
        pkg = store.get_directory(name)
        if pkg is None:
            return err(f"directory {name!r} not found", 404)
        body = request.get_json(silent=True) or {}
        base_rev = body.get("base_rev", pkg["rev"])
        changes = [{"op": "remove", "sha": e["sha"]} for e in pkg["entries"]]
        if not changes:
            return err("directory already empty", 400, code="empty_change")
        try:
            result = store.submit(name, base_rev, changes, note=f"unpublish {name}")
        except RevisionConflict as exc:
            return err(str(exc), 409, code="revision_conflict", current_rev=store.stats()["rev"])
        return jsonify(result), 201

    # ------------------------------------------------------------ health
    @app.get("/health")
    def health() -> tuple[Response, int]:
        writable = store.objects_dir_writable()
        integrity = store.integrity()
        status_code = 200 if writable and integrity.ok else 503
        return (
            jsonify(
                {
                    "status": "ok" if status_code == 200 else "degraded",
                    "objects_dir_writable": writable,
                    "objects_dir": store.objects_dir,
                    "integrity": integrity.as_dict(),
                    **store.stats(),
                }
            ),
            status_code,
        )

    @app.get("/api/state")
    def full_state() -> Response:
        return jsonify(
            {
                "directories": store.list_directories(),
                "stats": store.stats(),
                "revisions": store.list_revisions(limit=50)["revisions"],
            }
        )

    # ----------------------------------------------------------- frontend
    @app.get("/")
    def index() -> Response:
        return send_from_directory(static_dir, "index.html")

    @app.get("/<path:path>")
    def static_files(path: str) -> Response:
        return send_from_directory(static_dir, path)

    return app
