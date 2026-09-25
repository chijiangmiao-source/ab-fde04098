"""Content-addressed block store with revisioned rebuild package directories.

All mutations to directory edges, object files and reference counts happen
inside one exclusive ``flock`` boundary (``_transaction``), and every file is
fsynced before the metadata pointer is swapped, so a crash never leaves a
live package directory pointing at a missing object.

Garbage collection is two-phase, also crash safe:

1. ``mark``  - zero-reference objects get a ``<sha>.cand`` marker, nothing is
               deleted yet.
2. ``sweep`` - marked objects are removed and then their markers are removed;
               markers that regained references are dropped and the object is
               retained.

``recover`` runs at startup and converges any interrupted mark/sweep.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterator

CANDIDATE_SUFFIX = ".cand"
TMP_PREFIX = ".tmp-"


class StoreError(Exception):
    """Base class for store failures."""


class RevisionConflict(StoreError):
    """Submitted revision is not a child of the current revision."""


class DeletingObjectError(StoreError):
    """Publish tried to wire in an object that is mid-deletion."""


class UnknownObjectError(StoreError):
    """Publish referenced an object hash that does not exist in the store."""


class EmptyChangeError(StoreError):
    """A submission carried no effective changes."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: str, data: str) -> None:
    """Write ``data`` to ``path`` atomically (tmp + fsync + rename + fsync)."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@dataclasses.dataclass
class IntegrityReport:
    ok: bool
    missing: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": self.missing}


class Store:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.objects_dir = os.path.join(self.root, "objects")
        self.state_dir = os.path.join(self.root, "state")
        self.snapshots_dir = os.path.join(self.state_dir, "snapshots")
        self.lock_path = os.path.join(self.state_dir, "store.lock")
        self.manifest_path = os.path.join(self.state_dir, "manifest.json")
        self.journal_path = os.path.join(self.state_dir, "revisions.jsonl")
        os.makedirs(self.objects_dir, exist_ok=True)
        os.makedirs(self.snapshots_dir, exist_ok=True)
        # flock itself serializes across both threads and processes; the local
        # depth only lets the *same* thread re-enter without self-deadlock.
        self._local = threading.local()

    # ------------------------------------------------------------------ locks
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the single exclusive boundary for edges/objects/refcounts.

        Each thread takes out its own open-file-description and blocks on
        ``flock`` (flock is per open-file-description, so it serializes across
        threads as well as processes); only same-thread re-entry is reentrant.
        """
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        os.makedirs(self.state_dir, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self._local.fd = fd
        self._local.depth = 1
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                self._local.depth = 0
                self._local.fd = None

    # -------------------------------------------------------------- manifest
    def _empty_manifest(self) -> dict[str, Any]:
        return {
            "rev": 0,
            "directories": {},
            "refcount": {},
            "updated_at": utc_now(),
        }

    def _load_manifest(self) -> dict[str, Any]:
        try:
            with open(self.manifest_path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return self._empty_manifest()

    def _object_path(self, sha: str) -> str:
        return os.path.join(self.objects_dir, sha)

    def _candidate_path(self, sha: str) -> str:
        return os.path.join(self.objects_dir, sha + CANDIDATE_SUFFIX)

    def object_exists(self, sha: str) -> bool:
        return os.path.isfile(self._object_path(sha))

    def is_candidate(self, sha: str) -> bool:
        return os.path.isfile(self._candidate_path(sha))

    # ----------------------------------------------------------------- setup
    def initialize(self) -> None:
        with self._locked():
            if not os.path.exists(self.manifest_path):
                _atomic_write(
                    self.manifest_path, json.dumps(self._empty_manifest(), indent=2)
                )
                _fsync_dir(self.state_dir)
            self._recover_locked()

    def recover(self) -> dict[str, Any]:
        """Externally visible crash-recovery/ convergence entry point."""
        with self._locked():
            return self._recover_locked()

    def _recover_locked(self) -> dict[str, Any]:
        """Converge after an interrupted mark/sweep/upload.

        Invariant preserved: every sha referenced by a live directory has an
        object file on disk.
        """
        removed_candidates: list[str] = []
        released_candidates: list[str] = []
        swept_tmp = 0

        # 1. Uploads interrupted before their atomic rename can never be
        #    referenced; drop them.
        for name in os.listdir(self.objects_dir):
            if name.startswith(TMP_PREFIX):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.objects_dir, name))
                    swept_tmp += 1

        manifest = self._load_manifest()
        refcount = manifest.get("refcount", {})

        # 2. Finish / resolve any interrupted GC candidates.
        for name in os.listdir(self.objects_dir):
            if not name.endswith(CANDIDATE_SUFFIX):
                continue
            sha = name[: -len(CANDIDATE_SUFFIX)]
            marker = self._candidate_path(sha)
            obj = self._object_path(sha)
            if refcount.get(sha, 0) > 0:
                # A live package references it again (new publish is rejected
                # while marked, but be defensive): marker is stale.
                if os.path.exists(obj):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(marker)
                    released_candidates.append(sha)
                    continue
                # Referenced but the bytes are gone: cannot fabricate them.
                # Leave the marker so integrity check reports the problem
                # loudly instead of silently corrupting a live package.
                continue
            # Zero refs: finish the interrupted removal.
            if os.path.exists(obj):
                os.unlink(obj)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(marker)
            removed_candidates.append(sha)
        if removed_candidates or released_candidates or swept_tmp:
            _fsync_dir(self.objects_dir)

        # 3. Repair the audit journal from immutable snapshots if a crash tore
        #    the append after the manifest commit. Snapshots ahead of the
        #    manifest never committed: drop them instead of logging them.
        self._reconcile_journal_locked(manifest)

        return {
            "removed_candidates": removed_candidates,
            "released_candidates": released_candidates,
            "swept_tmp_uploads": swept_tmp,
        }

    def _snapshot_path(self, rev: int) -> str:
        return os.path.join(self.snapshots_dir, f"{rev:012d}.json")

    def _reconcile_journal_locked(self, manifest: dict[str, Any]) -> None:
        current_rev = manifest["rev"]
        revs_on_disk: set[int] = set()
        for name in os.listdir(self.snapshots_dir):
            if name.endswith(".json"):
                with contextlib.suppress(ValueError):
                    revs_on_disk.add(int(name[:-5]))
        logged: set[int] = set()
        if os.path.exists(self.journal_path):
            with open(self.journal_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    with contextlib.suppress(json.JSONDecodeError, KeyError):
                        logged.add(json.loads(line)["rev"])
        # Only committed snapshots (rev <= manifest pointer) may be journaled.
        committed = {r for r in revs_on_disk if r <= current_rev}
        uncommitted = sorted(r for r in revs_on_disk if r > current_rev)
        for rev in uncommitted:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._snapshot_path(rev))
        missing = sorted(r for r in committed if r not in logged)
        if not missing:
            if uncommitted:
                _fsync_dir(self.snapshots_dir)
            return
        with open(self.journal_path, "a", encoding="utf-8") as journal:
            for rev in missing:
                try:
                    with open(self._snapshot_path(rev), encoding="utf-8") as fh:
                        snap = json.load(fh)
                except FileNotFoundError:
                    continue
                journal.write(
                    json.dumps(
                        {
                            "rev": rev,
                            "parent_rev": snap["parent_rev"],
                            "note": snap.get("note", ""),
                            "changes": snap.get("changes", []),
                            "timestamp": snap.get("timestamp", utc_now()),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())

    # ---------------------------------------------------------------- blocks
    def put_block(self, content: bytes, expected_sha: str | None = None) -> str:
        sha = hashlib.sha256(content).hexdigest()
        if expected_sha and expected_sha != sha:
            raise StoreError("content does not match expected sha-256")
        path = self._object_path(sha)
        if os.path.isfile(path):
            return sha
        directory = self.objects_dir
        fd, tmp = tempfile.mkstemp(prefix=TMP_PREFIX, dir=directory)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            # Publish must never wire in a deleting object; an upload cannot
            # resurrect one either - sweep must finish (it has zero refs).
            if os.path.exists(self._candidate_path(sha)):
                os.unlink(tmp)
                raise DeletingObjectError(
                    f"object {sha} is marked for deletion; retry after GC settles"
                )
            os.replace(tmp, path)
            _fsync_dir(directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return sha

    def read_block(self, sha: str) -> bytes:
        with self._locked():
            if not self._valid_sha(sha):
                raise UnknownObjectError(sha)
            if self.is_candidate(sha):
                raise UnknownObjectError(sha)
            path = self._object_path(sha)
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except FileNotFoundError:
                raise UnknownObjectError(sha) from None

    def block_status(self, sha: str) -> dict[str, Any]:
        """Readability + retention reason for a block."""
        with self._locked():
            manifest = self._load_manifest()
            referrers = sorted(
                name
                for name, pkg in manifest["directories"].items()
                if sha in pkg["entries"]
            )
            exists = self.object_exists(sha)
            candidate = self.is_candidate(sha)
            refs = manifest["refcount"].get(sha, 0)
            if candidate:
                state = "deleting"
                reason = "最后引用已撤下，块已进入删除候选，等待清理移除"
            elif exists and referrers:
                state = "retained"
                reason = (
                    f"仍被 {len(referrers)} 个目录引用: {', '.join(referrers)}"
                )
            elif exists:
                state = "orphan"
                reason = "对象存在但当前无目录引用（等待 GC 标记）"
            else:
                state = "missing"
                reason = "无此对象：最后引用撤下并清理后不可读取"
            return {
                "sha": sha,
                "exists": exists,
                "readable": exists and not candidate,
                "state": state,
                "refcount": refs,
                "referrers": referrers,
                "reason": reason,
            }

    @staticmethod
    def _valid_sha(sha: str) -> bool:
        return len(sha) == 64 and all(c in "0123456789abcdef" for c in sha.lower())

    # -------------------------------------------------------------- submit
    def submit(
        self,
        directory: str,
        base_rev: int,
        changes: list[dict[str, str]],
        note: str = "",
    ) -> dict[str, Any]:
        """Apply reviewer add/remove changes as a new numbered revision.

        Stale ``base_rev`` is rejected *before* anything is touched.
        """
        with self._locked():
            manifest = self._load_manifest()

            # Stable rejection of expired revisions - check first, mutate never.
            if base_rev != manifest["rev"]:
                raise RevisionConflict(
                    f"base revision {base_rev} is stale; current is {manifest['rev']}"
                )

            # Validate the whole batch up front: unknown or deleting objects
            # reject the submission without changing edges/counts/objects.
            normalized: list[dict[str, str]] = []
            seen_adds: set[str] = set()
            packages = manifest["directories"]
            entries = packages.setdefault(directory, {"entries": {}})["entries"]
            effective = 0
            for ch in changes:
                op = ch.get("op")
                sha = (ch.get("sha") or "").lower()
                if op not in ("add", "remove") or not self._valid_sha(sha):
                    raise StoreError(f"invalid change: {ch!r}")
                if op == "add":
                    if sha in entries or sha in seen_adds:
                        continue  # idempotent duplicate add, no effect
                    seen_adds.add(sha)
                    if self.is_candidate(sha):
                        raise DeletingObjectError(
                            f"refusing to wire deleting object {sha} into a package"
                        )
                    if not self.object_exists(sha):
                        raise UnknownObjectError(sha)
                    normalized.append(
                        {"op": "add", "sha": sha, "name": ch.get("name") or sha[:12]}
                    )
                    effective += 1
                else:
                    if sha not in entries:
                        continue  # idempotent duplicate remove, no effect
                    normalized.append({"op": "remove", "sha": sha})
                    effective += 1

            if effective == 0:
                raise EmptyChangeError("submission contains no effective changes")

            refcount = Counter(manifest["refcount"])
            applied: list[dict[str, str]] = []
            for ch in normalized:
                sha = ch["sha"]
                if ch["op"] == "add":
                    st = os.stat(self._object_path(sha))
                    entries[sha] = {
                        "sha": sha,
                        "name": ch.get("name") or sha[:12],
                        "size": st.st_size,
                        "added_at": utc_now(),
                    }
                    refcount[sha] += 1
                else:
                    entries.pop(sha, None)
                    refcount[sha] -= 1
                    if refcount[sha] <= 0:
                        refcount.pop(sha, None)
                applied.append(ch)

            new_rev = manifest["rev"] + 1
            manifest["rev"] = new_rev
            manifest["refcount"] = dict(sorted(refcount.items()))
            manifest["updated_at"] = utc_now()
            packages[directory]["name"] = directory

            summary = {
                "added": sum(1 for c in applied if c["op"] == "add"),
                "removed": sum(1 for c in applied if c["op"] == "remove"),
            }
            snapshot = {
                "rev": new_rev,
                "parent_rev": base_rev,
                "directory": directory,
                "note": note,
                "changes": applied,
                "summary": summary,
                "timestamp": manifest["updated_at"],
                "directories": manifest["directories"],
                "refcount": manifest["refcount"],
            }

            # Immutable snapshot lands before the pointer moves, journal last.
            _atomic_write(
                self._snapshot_path(new_rev),
                json.dumps(snapshot, ensure_ascii=False, indent=2),
            )
            _atomic_write(
                self.manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            with open(self.journal_path, "a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(
                        {
                            "rev": new_rev,
                            "parent_rev": base_rev,
                            "directory": directory,
                            "note": note,
                            "changes": applied,
                            "summary": summary,
                            "timestamp": manifest["updated_at"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())

            return {
                "rev": new_rev,
                "parent_rev": base_rev,
                "directory": directory,
                "changes": applied,
                "summary": summary,
            }

    # -------------------------------------------------------------------- GC
    def gc_mark(self) -> dict[str, Any]:
        """Phase 1: leave candidate markers on zero-reference objects."""
        with self._locked():
            manifest = self._load_manifest()
            refcount = manifest.get("refcount", {})
            marked: list[str] = []
            for name in os.listdir(self.objects_dir):
                if name.endswith(CANDIDATE_SUFFIX) or name.startswith(TMP_PREFIX):
                    continue
                sha = name
                if not self._valid_sha(sha):
                    continue
                if refcount.get(sha, 0) == 0:
                    marker = self._candidate_path(sha)
                    if not os.path.exists(marker):
                        _atomic_write(
                            marker,
                            json.dumps(
                                {
                                    "sha": sha,
                                    "reason": "zero references",
                                    "marked_at": utc_now(),
                                },
                                indent=2,
                            ),
                        )
                    marked.append(sha)
            return {"phase": "mark", "marked": sorted(marked)}

    def gc_sweep(self) -> dict[str, Any]:
        """Phase 2: remove marked objects, then their markers."""
        with self._locked():
            manifest = self._load_manifest()
            refcount = manifest.get("refcount", {})
            removed: list[str] = []
            released: list[str] = []
            for name in sorted(os.listdir(self.objects_dir)):
                if not name.endswith(CANDIDATE_SUFFIX):
                    continue
                sha = name[: -len(CANDIDATE_SUFFIX)]
                marker = self._candidate_path(sha)
                obj = self._object_path(sha)
                if refcount.get(sha, 0) > 0:
                    if os.path.exists(obj):
                        os.unlink(marker)
                        released.append(sha)
                    # Referenced but bytes missing: never drop the marker
                    # silently; integrity check must keep reporting it.
                    continue
                # Remove bytes first, marker second - restart resumes if torn.
                if os.path.exists(obj):
                    os.unlink(obj)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(marker)
                removed.append(sha)
            if removed or released:
                _fsync_dir(self.objects_dir)
            return {
                "phase": "sweep",
                "removed": sorted(removed),
                "released": sorted(released),
            }

    def gc(self, mode: str = "auto") -> dict[str, Any]:
        if mode == "mark":
            return self.gc_mark()
        if mode == "sweep":
            return self.gc_sweep()
        if mode == "auto":
            marked = self.gc_mark()
            swept = self.gc_sweep()
            return {"phase": "auto", "marked": marked["marked"], **swept}
        raise StoreError(f"unknown gc mode {mode!r}")

    # ---------------------------------------------------------------- reads
    def list_directories(self) -> dict[str, Any]:
        with self._locked():
            manifest = self._load_manifest()
            pkgs = []
            for name, pkg in sorted(manifest["directories"].items()):
                pkgs.append(
                    {
                        "name": name,
                        "entry_count": len(pkg["entries"]),
                        "entries": [
                            pkg["entries"][sha] for sha in sorted(pkg["entries"])
                        ],
                    }
                )
            return {"rev": manifest["rev"], "directories": pkgs}

    def get_directory(self, name: str) -> dict[str, Any] | None:
        with self._locked():
            manifest = self._load_manifest()
            pkg = manifest["directories"].get(name)
            if pkg is None:
                return None
            return {
                "rev": manifest["rev"],
                "name": name,
                "entries": [pkg["entries"][sha] for sha in sorted(pkg["entries"])],
            }

    def list_revisions(self, limit: int = 100) -> dict[str, Any]:
        with self._locked():
            manifest = self._load_manifest()
            rows: list[dict[str, Any]] = []
            if os.path.exists(self.journal_path):
                with open(self.journal_path, encoding="utf-8") as fh:
                    rows = [json.loads(line) for line in fh if line.strip()]
            return {"rev": manifest["rev"], "revisions": rows[-limit:]}

    def stats(self) -> dict[str, Any]:
        with self._locked():
            manifest = self._load_manifest()
            objects = [
                n
                for n in os.listdir(self.objects_dir)
                if self._valid_sha(n) and os.path.isfile(self._object_path(n))
            ]
            candidates = [
                n[: -len(CANDIDATE_SUFFIX)]
                for n in os.listdir(self.objects_dir)
                if n.endswith(CANDIDATE_SUFFIX)
            ]
            return {
                "rev": manifest["rev"],
                "directories": len(manifest["directories"]),
                "objects": len(objects),
                "candidates": sorted(candidates),
                "refcount": manifest["refcount"],
            }

    def integrity(self) -> IntegrityReport:
        with self._locked():
            manifest = self._load_manifest()
            missing: list[str] = []
            for pkg in manifest["directories"].values():
                for sha in pkg["entries"]:
                    if not self.object_exists(sha) or self.is_candidate(sha):
                        missing.append(sha)
            return IntegrityReport(ok=not missing, missing=sorted(set(missing)))

    def objects_dir_writable(self) -> bool:
        """Health probe: can we create/fsync/unlink in the object directory?"""
        probe = os.path.join(self.objects_dir, f".write-probe-{os.getpid()}")
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, b"ok")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.unlink(probe)
            _fsync_dir(self.objects_dir)
            return True
        except OSError as exc:
            if exc.errno in (errno.EROFS, errno.EACCES, errno.ENOSPC):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(probe)
            return False

    def crash_simulate_torn_removal(self, sha: str) -> None:
        """Test hook: emulate a crash between unlink(object) and unlink(marker)."""
        with self._locked():
            marker = self._candidate_path(sha)
            obj = self._object_path(sha)
            if not os.path.exists(marker):
                _atomic_write(
                    marker,
                    json.dumps(
                        {"sha": sha, "reason": "simulated", "marked_at": utc_now()}
                    ),
                )
            if os.path.exists(obj):
                os.unlink(obj)
                _fsync_dir(self.objects_dir)


# Small monotonic helper used by smoke scripts that want wall-clock pacing.
def now_epoch() -> float:
    return time.time()
