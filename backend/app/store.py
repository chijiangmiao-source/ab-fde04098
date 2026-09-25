"""内容寻址块存储与重建包目录管理。

设计要点（与验收语义一一对应）：

* 目录边（manifest 修订）、对象文件（objects/）、引用计数（refs.json）
  全部置于同一把排他文件锁 ``store.lock`` 之后，保证三者在同一持久化
  排他边界内一致变更。
* 对象按 SHA-256 内容寻址、天然去重；引用计数记录“有多少个已发布
  包目录引用该块”。
* 只有零引用块可回收；清理分两步——先在 ``staging/`` 留下候选标记
  （``.gc-candidate``），再把对象文件移入 ``staging/``，随后删除；
  发布路径从不读取 ``staging/`` 中的旧对象，绝不会重新挂接删除中的对象。
* 标记阶段或移除阶段中断后，重启时 :meth:`PackageStore.recover` 会收敛：
  存活包始终不指向缺失内容（对象在引用计数归零前不会离开 objects/）。
* 提交采用单调递增修订号 + 乐观并发（expected_revision），过期修订被
  稳定拒绝（``StaleRevision``），且不改变既有目录、引用数或对象。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

DRAFT_REVISION = 0
FIRST_REVISION = 1
CANDIDATE_MARKER = ".gc-candidate"
REFS_NAME = "refs.json"
MANIFEST_NAME = "manifest.json"


def sha256_text(text: str) -> str:
    """对文本块计算规范化的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_path(objects_dir: str, digest: str) -> str:
    """对象分桶路径：objects/ab/cdef... ，避免单目录条目过多。"""
    return os.path.join(objects_dir, digest[:2], digest[2:])


class StaleRevision(Exception):
    """提交所基于的修订号已过期（乐观并发冲突）。"""


class MissingBlocks(Exception):
    """提交引用了存储中不存在的块（健康/一致性问题，正常流程不会发生）。"""


@dataclass
class CommitResult:
    package: str
    revision: int
    added: List[str]
    removed: List[str]
    retained: List[str]


class PackageStore:
    """文件系统支持的包/块存储。所有公共方法都在排他锁内执行。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.objects_dir = os.path.join(self.root, "objects")
        self.packages_dir = os.path.join(self.root, "packages")
        self.staging_dir = os.path.join(self.root, "staging")
        self.lock_path = os.path.join(self.root, "store.lock")
        self._tlocal = threading.local()
        for path in (self.root, self.objects_dir, self.packages_dir, self.staging_dir):
            os.makedirs(path, exist_ok=True)
        # 锁文件本身持久存在，仅用于 fcntl 互斥。
        with open(self.lock_path, "a"):
            pass

    # ------------------------------------------------------------------ 锁

    @contextlib.contextmanager
    def _locked(self):
        """进程间排他（fcntl）+ 线程间排他（可重入）。"""
        depth = getattr(self._tlocal, "depth", 0)
        reentrant = depth > 0
        if not reentrant:
            lock_file = open(self.lock_path, "r")
            # 阻塞等待，保证所有读者/写者在同一排他边界串行化。
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._tlocal.lock_file = lock_file
        self._tlocal.depth = depth + 1
        try:
            yield
        finally:
            self._tlocal.depth -= 1
            if self._tlocal.depth == 0:
                lock_file = self._tlocal.lock_file
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                self._tlocal.lock_file = None

    # ------------------------------------------------------------ 原子写

    def _atomic_write(self, path: str, data: str) -> None:
        """同目录临时文件 + fsync + os.replace，崩溃安全的原子写。"""
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            self._fsync_dir(directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _fsync_dir(path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # 某些文件系统不支持目录 fsync，不影响正确性。
            pass

    # ------------------------------------------------------------ 状态 IO

    def _refs_path(self) -> str:
        return os.path.join(self.root, REFS_NAME)

    def _load_state(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """加载 refs.json 与全部包 manifest（必须持锁调用）。"""
        try:
            with open(self._refs_path(), "r", encoding="utf-8") as fh:
                refs = json.load(fh)
        except FileNotFoundError:
            refs = {}
        if not isinstance(refs, dict):
            refs = {}

        packages: Dict[str, Any] = {}
        if os.path.isdir(self.packages_dir):
            for name in os.listdir(self.packages_dir):
                manifest = os.path.join(self.packages_dir, name, MANIFEST_NAME)
                if os.path.isfile(manifest):
                    try:
                        with open(manifest, "r", encoding="utf-8") as fh:
                            packages[name] = json.load(fh)
                    except (ValueError, OSError):
                        # 损坏/半截写的 manifest（mkstemp+replace 正常不会出现，
                        # 这里仅作防御）：忽略，随后续提交重建。
                        continue
        return refs, packages

    def _save_refs(self, refs: Dict[str, Any]) -> None:
        body = json.dumps(refs, sort_keys=True, indent=2, ensure_ascii=False)
        self._atomic_write(self._refs_path(), body)

    @staticmethod
    def _package_dir(name: str) -> str:
        # 包名仅允许文件系统安全字符，杜绝路径穿越。
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise ValueError(f"非法包名: {name!r}")
        return name  # 调用方拼接 packages_dir

    # ------------------------------------------------------------ 恢复

    def recover(self) -> Dict[str, Any]:
        """启动时在排他锁内收敛状态。

        做两件事：

        1. **引用计数对账**：已提交的包目录（manifest）是权威状态，
           ``refs.json`` 是其派生计数。若上次进程在“manifest 落盘之后、
           refs 落盘之前”崩溃，这里以全部存活包目录重算引用计数并写回，
           消除跨文件原子写无法覆盖的瞬时窗口。
        2. **GC 候选收敛**：对 ``staging/`` 中残留的候选标记——
           对象仍在 objects/ 则移动后删除，已在 staging/ 则删除残留。

        因为对象总是先于目录边写入、引用计数归零后才允许被 GC，任何存活
        包都不可能指向缺失内容。整个过程幂等。
        """
        with self._locked():
            return self._recover_locked()

    def _reconcile_refs_locked(self, packages: Dict[str, Any]) -> Dict[str, int]:
        """以包目录为权威重算引用计数，必要时原子写回。"""
        recomputed: Dict[str, int] = {}
        dangling: List[str] = []
        for pkg in packages.values():
            for digest in pkg.get("blocks", []):
                if not _is_hex_digest(digest):
                    continue
                recomputed[digest] = recomputed.get(digest, 0) + 1
                if not os.path.isfile(digest_path(self.objects_dir, digest)):
                    dangling.append(digest)
        refs, _ = self._load_state()
        if refs != recomputed:
            self._save_refs(recomputed)
        return recomputed

    def _recover_locked(self) -> Dict[str, Any]:
        removed: List[str] = []
        cancelled: List[str] = []
        refs, packages = self._load_state()
        refs_int = {d: int(c) for d, c in refs.items()}
        reconciled = self._reconcile_refs_locked(packages)
        refs_changed = reconciled != refs_int
        if refs_changed:
            refs_int = reconciled
        dangling = [
            d for d in refs_int
            if not os.path.isfile(digest_path(self.objects_dir, d))
        ]
        if not os.path.isdir(self.staging_dir):
            return {"removed": removed, "cancelled": cancelled,
                    "refs_reconciled": refs_changed, "dangling": dangling}
        for entry in sorted(os.listdir(self.staging_dir)):
            if not entry.startswith(CANDIDATE_MARKER):
                continue
            marker_path = os.path.join(self.staging_dir, entry)
            digest = entry[len(CANDIDATE_MARKER) + 1 :].lower()
            if refs_int.get(digest, 0) > 0:
                # 理论上不可达：标记仅在引用数归零时写下。防御性撤销标记。
                self._remove_file(marker_path)
                cancelled.append(digest)
                continue

            obj_path = digest_path(self.objects_dir, digest)
            staged = os.path.join(self.staging_dir, digest)
            if os.path.isfile(obj_path):
                # 阶段 2 未完成：移动后删除。
                self._move_for_delete(obj_path, staged)
            if os.path.isfile(staged):
                self._remove_file(staged)
            self._remove_file(marker_path)
            removed.append(digest)
        return {"removed": removed, "cancelled": cancelled,
                "refs_reconciled": refs_changed, "dangling": dangling}

    # ------------------------------------------------------------ 对象读

    def read_block(self, digest: str) -> Optional[str]:
        """按摘要读取块文本；不存在（含已回收）返回 None。绝不读 staging。"""
        if not _is_hex_digest(digest):
            return None
        digest = digest.lower()
        with self._locked():
            path = digest_path(self.objects_dir, digest)
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()

    def has_block(self, digest: str) -> bool:
        if not _is_hex_digest(digest):
            return False
        with self._locked():
            return os.path.isfile(digest_path(self.objects_dir, digest.lower()))

    def put_loose_block(self, text: str) -> str:
        """供前端“新增块”使用：写入一个去重对象但不产生引用。

        已存在则直接返回摘要（去重不覆盖）。无引用的游离对象可被 GC 回收。
        """
        digest = sha256_text(text)
        with self._locked():
            self._store_object_locked(digest, text)
            return digest

    def _store_object_locked(self, digest: str, text: str) -> bool:
        """写入对象（若不存在）。返回是否为新建。发布只从 objects/ 挂接。"""
        path = digest_path(self.objects_dir, digest)
        if os.path.isfile(path):
            return False
        # 先写临时文件再原子落位；任何时候都不会把 staging 里的旧对象
        # 重新接回 objects/（临时文件在目标分桶目录内，与 staging 无关）。
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".obj-tmp-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return True

    # ------------------------------------------------------------ 提交

    def commit(
        self,
        package: str,
        block_texts: Optional[Iterable[str]] = None,
        block_digests: Optional[Iterable[str]] = None,
        expected_revision: Optional[int] = None,
        reason: str = "",
    ) -> CommitResult:
        """提交一个包目录的新修订。

        ``block_texts`` 中的文本会被规范化哈希入库（去重）；也可直接传
        已知 ``block_digests``（必须已存在于 objects/）。两者取并集并去重。
        ``expected_revision`` 为调用方读取到的当前修订号；不匹配则抛
        :class:`StaleRevision`，且不产生任何副作用。
        """
        name = self._package_dir(package)
        wanted: Set[str] = set()
        texts: Dict[str, str] = {}
        for text in block_texts or []:
            d = sha256_text(text)
            wanted.add(d)
            texts[d] = text
        for d in block_digests or []:
            if not _is_hex_digest(d):
                raise ValueError(f"非法摘要: {d!r}")
            wanted.add(d.lower())

        with self._locked():
            refs, packages = self._load_state()
            current = packages.get(name)
            current_rev = int(current["revision"]) if current else DRAFT_REVISION
            if expected_revision is not None and expected_revision != current_rev:
                # 稳定拒绝：不触碰目录、引用数与对象。
                raise StaleRevision(
                    f"过期修订提交: 基于 {expected_revision}，当前为 {current_rev}"
                )

            old_set: Set[str] = set(current["blocks"]) if current else set()

            # 新增对象必须真实存在于 objects/ 后才更新引用计数与目录。
            missing = [d for d in wanted if not os.path.isfile(
                digest_path(self.objects_dir, d)) and d not in texts]
            if missing:
                raise MissingBlocks(f"缺失对象: {sorted(missing)}")

            # 1) 先把新对象持久化（去重）。
            for d, text in texts.items():
                self._store_object_locked(d, text)

            added = sorted(wanted - old_set)
            removed = sorted(old_set - wanted)
            retained = sorted(old_set & wanted)

            # 2) 在同一临界区内更新引用计数。
            for d in added:
                refs[d] = refs.get(d, 0) + 1
            for d in removed:
                count = refs.get(d, 0) - 1
                if count <= 0:
                    refs.pop(d, None)
                else:
                    refs[d] = count

            new_revision = current_rev + 1
            manifest = {
                "package": name,
                "revision": new_revision,
                "blocks": sorted(wanted),
                "reason": reason or "",
                "committed_at": _utc_now_iso(),
            }

            # 3) 原子落盘 manifest 与 refs（同一把锁保护，崩溃时锁释放后
            #    recover + 下次提交会重新收敛，存活包永不指向缺失内容：
            #    对象先于引用写入，引用归零后对象才允许被 GC）。
            manifest_path = os.path.join(self.packages_dir, name, MANIFEST_NAME)
            self._atomic_write(manifest_path, json.dumps(
                manifest, sort_keys=True, indent=2, ensure_ascii=False))
            self._save_refs(refs)
            return CommitResult(name, new_revision, added, removed, retained)

    # ------------------------------------------------------------ 查询

    def get_package(self, package: str) -> Optional[Dict[str, Any]]:
        name = self._package_dir(package)
        with self._locked():
            _, packages = self._load_state()
            pkg = packages.get(name)
            if pkg is None:
                return None
            return dict(pkg)

    def list_packages(self) -> List[Dict[str, Any]]:
        with self._locked():
            _, packages = self._load_state()
            return [dict(packages[n]) for n in sorted(packages)]

    def ref_count(self, digest: str) -> int:
        if not _is_hex_digest(digest):
            return 0
        with self._locked():
            refs, _ = self._load_state()
            return int(refs.get(digest.lower(), 0))

    def refs_snapshot(self) -> Dict[str, int]:
        with self._locked():
            refs, _ = self._load_state()
            return {d: int(c) for d, c in refs.items()}

    def summary(self) -> Dict[str, Any]:
        """复核员视角：每个包的摘要、修订与引用数。"""
        with self._locked():
            refs, packages = self._load_state()
            items = []
            for name in sorted(packages):
                pkg = packages[name]
                blocks = list(pkg.get("blocks", []))
                items.append({
                    "package": name,
                    "revision": int(pkg.get("revision", 0)),
                    "block_count": len(blocks),
                    "blocks": blocks,
                    "reference_counts": {d: int(refs.get(d, 0)) for d in blocks},
                    "reason": pkg.get("reason", ""),
                    "committed_at": pkg.get("committed_at", ""),
                })
            return {
                "packages": items,
                "total_packages": len(items),
                "total_referenced_blocks": len(refs),
            }

    # ------------------------------------------------------------ GC

    def garbage_collect(self) -> Dict[str, Any]:
        """两阶段回收零引用块：先候选标记，再移除文件。

        返回每个阶段处理的摘要，便于测试在“标记后/移除前”注入崩溃。
        """
        with self._locked():
            refs, packages = self._load_state()
            live: Set[str] = set(refs.keys())  # refs 只含计数 > 0 的块
            # 防御性再核对：任何存活包目录中的块都绝不回收，即使引用计数
            # 因外部损坏而缺失（存活包始终不指向缺失内容是硬不变量）。
            for pkg in packages.values():
                live.update(pkg.get("blocks", []))

            # 游离对象（put_loose_block 写入但无目录引用）也属于零引用，
            # 可被回收；扫描 objects/ 找出所有非存活对象。
            candidates = [d for d in self._iter_objects() if d not in live]

            marked: List[str] = []
            removed: List[str] = []
            for digest in sorted(candidates):
                if self._mark_candidate_locked(digest):
                    marked.append(digest)
            # 标记完成后再统一移除；若在标记与移除之间中断，标记留在
            # staging，重启 recover 收敛。
            for digest in marked:
                if self._remove_candidate_locked(digest):
                    removed.append(digest)
            return {"marked": marked, "removed": removed}

    def mark_candidates_only(self) -> List[str]:
        """仅执行第一阶段（留下候选标记），供崩溃注入/测试使用。"""
        with self._locked():
            refs, packages = self._load_state()
            live: Set[str] = set(refs.keys())
            for pkg in packages.values():
                live.update(pkg.get("blocks", []))
            marked = []
            for digest in sorted(d for d in self._iter_objects() if d not in live):
                if self._mark_candidate_locked(digest):
                    marked.append(digest)
            return marked

    def _mark_candidate_locked(self, digest: str) -> bool:
        marker = os.path.join(self.staging_dir, f"{CANDIDATE_MARKER}-{digest}")
        if os.path.exists(marker):
            return False
        meta = json.dumps({"digest": digest, "marked_at": time.time()})
        # 标记写在 staging/，对象此时仍在 objects/，发布读取不受影响。
        self._atomic_write(marker, meta)
        return True

    def _remove_candidate_locked(self, digest: str) -> bool:
        marker = os.path.join(self.staging_dir, f"{CANDIDATE_MARKER}-{digest}")
        obj_path = digest_path(self.objects_dir, digest)
        staged = os.path.join(self.staging_dir, digest)
        if not os.path.exists(marker):
            return False
        if os.path.isfile(obj_path):
            # 先同设备移动到 staging（发布路径不再可见），再删除文件。
            self._move_for_delete(obj_path, staged)
        self._remove_file(staged)
        self._remove_file(marker)
        return True

    def _move_for_delete(self, src: str, staged_dst: str) -> None:
        try:
            os.replace(src, staged_dst)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return
            raise
        # 清理可能变空的分桶目录。
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(src))

    @staticmethod
    def _remove_file(path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    def _iter_objects(self) -> Iterable[str]:
        if not os.path.isdir(self.objects_dir):
            return
        for bucket in sorted(os.listdir(self.objects_dir)):
            bdir = os.path.join(self.objects_dir, bucket)
            if not (len(bucket) == 2 and os.path.isdir(bdir)):
                continue
            for tail in sorted(os.listdir(bdir)):
                digest = bucket + tail
                if _is_hex_digest(digest) and os.path.isfile(os.path.join(bdir, tail)):
                    yield digest

    # ------------------------------------------------------------ 健康

    def objects_writable(self) -> bool:
        """探测对象目录是否可写（健康接口上报）。"""
        probe = os.path.join(self.objects_dir, ".write-probe")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
                fh.flush()
                os.fsync(fh.fileno())
            os.unlink(probe)
            return True
        except OSError:
            return False

    def storage_stats(self) -> Dict[str, Any]:
        with self._locked():
            refs, packages = self._load_state()
            objects = list(self._iter_objects())
            return {
                "objects_on_disk": len(objects),
                "referenced_blocks": len(refs),
                "packages": len(packages),
            }


def _is_hex_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    lowered = value.lower()
    return all(c in "0123456789abcdef" for c in lowered)


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
