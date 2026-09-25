"""存储层验收测试：去重、共享引用保留、两阶段 GC、故障重启收敛、修订冲突。"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import (  # noqa: E402
    CANDIDATE_MARKER,
    PackageStore,
    StaleRevision,
    digest_path,
    sha256_text,
)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="calstore-")
        self.store = PackageStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fresh_store(self):
        """模拟进程重启：新实例 + 启动恢复。"""
        store = PackageStore(self.root)
        store.recover()
        return store

    def assertOnDisk(self, digest):
        self.assertTrue(os.path.isfile(digest_path(self.store.objects_dir, digest)),
                        f"对象 {digest[:12]} 应存在于 objects/")

    def assertNotOnDisk(self, digest):
        self.assertFalse(os.path.isfile(digest_path(self.store.objects_dir, digest)),
                         f"对象 {digest[:12]} 不应存在于 objects/")


class DedupAndRevisionTests(StoreTestBase):
    def test_same_content_deduplicated_and_sha256_addressed(self):
        text = "CAL-BLOCK v1\ngain=1.02\n"
        d = sha256_text(text)
        r1 = self.store.commit("pkg-a", block_texts=[text])
        r2 = self.store.commit("pkg-b", block_texts=[text])
        self.assertEqual(r1.added, [d])
        self.assertEqual(r2.added, [d])  # 同一摘要
        self.assertEqual(self.store.ref_count(d), 2)
        # 物理上只有一份对象文件
        objs = list(os.walk(self.store.objects_dir))
        files = [f for _, _, fs in objs for f in fs if not f.startswith(".")]
        self.assertEqual(len(files), 1)
        self.assertEqual(self.store.read_block(d), text)

    def test_revision_monotonic_and_stale_rejected_without_side_effects(self):
        r1 = self.store.commit("pkg-a", block_texts=["block-1"], expected_revision=0)
        self.assertEqual(r1.revision, 1)
        r2 = self.store.commit("pkg-a", block_texts=["block-1", "block-2"],
                               expected_revision=1)
        self.assertEqual(r2.revision, 2)

        # 过期修订（基于 1）必须被稳定拒绝
        ghost_text = "ghost-block-from-stale-commit"
        ghost_digest = sha256_text(ghost_text)
        refs_before = self.store.refs_snapshot()
        with self.assertRaises(StaleRevision):
            self.store.commit("pkg-a", block_texts=[ghost_text], expected_revision=1)

        pkg = self.store.get_package("pkg-a")
        self.assertEqual(pkg["revision"], 2)  # 目录未变
        self.assertEqual(self.store.refs_snapshot(), refs_before)  # 引用数未变
        self.assertIsNone(self.store.read_block(ghost_digest))  # 未写入任何对象
        self.assertNotOnDisk(ghost_digest)

    def test_concurrent_commits_serialized_by_lock(self):
        errors = []

        def worker(i):
            try:
                for j in range(20):
                    self.store.commit(f"pkg-{i}", block_texts=[f"b-{i}-{j}"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        self.assertEqual(errors, [])
        summary = self.store.summary()
        self.assertEqual(summary["total_packages"], 8)
        # 每个包最终只引用最后一个块（旧修订块零引用但尚未 GC，仍在盘上）
        self.assertEqual(summary["total_referenced_blocks"], 8)
        # GC 后盘上对象与引用计数严格一致
        self.store.garbage_collect()
        stats = self.store.storage_stats()
        self.assertEqual(stats["objects_on_disk"], stats["referenced_blocks"])
        self.assertEqual(stats["objects_on_disk"], 8)


class SharedReferenceRetentionTests(StoreTestBase):
    def test_unshare_then_last_reference_removal(self):
        shared = "SHARED CALIBRATION FRAME"
        only_a = "A-private-frame"
        only_b = "B-private-frame"
        ds, da, db = sha256_text(shared), sha256_text(only_a), sha256_text(only_b)

        self.store.commit("pkg-a", block_texts=[shared, only_a])
        self.store.commit("pkg-b", block_texts=[shared, only_b])
        self.assertEqual(self.store.ref_count(ds), 2)

        # 复核员撤下 pkg-a 的全部内容并清理
        ra = self.store.commit("pkg-a", block_texts=[], expected_revision=1)
        self.assertEqual(ra.removed, sorted([ds, da]))
        self.assertEqual(ra.retained, [])
        gc = self.store.garbage_collect()
        self.assertIn(da, gc["removed"])      # A 私有块零引用，被回收
        self.assertNotIn(ds, gc["removed"])   # 共享块仍被 B 引用，绝不回收

        # 另一包仍能通过接口语义（store.read_block）读取共享块
        self.assertEqual(self.store.read_block(ds), shared)
        self.assertEqual(self.store.ref_count(ds), 1)
        # 摘要里 A 不再含该块，B 仍含
        summary = self.store.summary()
        blocks_by_pkg = {p["package"]: p["blocks"] for p in summary["packages"]}
        self.assertNotIn(ds, blocks_by_pkg["pkg-a"])
        self.assertIn(ds, blocks_by_pkg["pkg-b"])
        # 引用统计仍保留该块（计数 1）
        self.assertIn(ds, self.store.refs_snapshot())

        # 撤下最后引用（pkg-b）并清理
        self.store.commit("pkg-b", block_texts=[], expected_revision=1)
        gc2 = self.store.garbage_collect()
        self.assertIn(ds, gc2["removed"])
        self.assertIn(db, gc2["removed"])
        self.assertIsNone(self.store.read_block(ds))  # 无法读取
        self.assertEqual(self.store.ref_count(ds), 0)
        # 目录与引用统计不再保留它
        self.assertNotIn(ds, self.store.refs_snapshot())
        for pkg in self.store.summary()["packages"]:
            self.assertNotIn(ds, pkg["blocks"])
        self.assertNotOnDisk(ds)

    def test_gc_never_reclaims_referenced_even_if_refs_tampered(self):
        text = "protected"
        d = sha256_text(text)
        self.store.commit("pkg-a", block_texts=[text])
        # 即使 refs.json 被外部清空，存活包目录中的块也不允许被回收
        with open(os.path.join(self.root, "refs.json"), "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        gc = self.store.garbage_collect()
        self.assertNotIn(d, gc["removed"])
        self.assertEqual(self.store.read_block(d), text)


class GarbageCollectionTwoPhaseTests(StoreTestBase):
    def test_mark_then_remove_and_marker_left_behind(self):
        d = sha256_text("loose")
        self.store.put_loose_block("loose")
        marked = self.store.mark_candidates_only()
        self.assertEqual(marked, [d])
        # 阶段 1 后：候选标记存在，对象仍在 objects/（仍可读）
        marker = os.path.join(self.store.staging_dir, f"{CANDIDATE_MARKER}-{d}")
        self.assertTrue(os.path.isfile(marker))
        self.assertOnDisk(d)

        # 单独执行第二阶段（模拟标记完成后进程继续执行移除）
        self.store._remove_candidate_locked(d)  # noqa: SLF001
        self.assertNotOnDisk(d)
        self.assertFalse(os.path.isfile(marker))

    def test_restart_after_mark_crash_converges(self):
        # A、B 共享块；A 撤下；制造一个零引用候选
        shared, priv = "shared-frame", "a-private"
        ds, dp = sha256_text(shared), sha256_text(priv)
        self.store.commit("pkg-a", block_texts=[shared, priv])
        self.store.commit("pkg-b", block_texts=[shared])
        self.store.commit("pkg-a", block_texts=[shared], expected_revision=1)
        marked = self.store.mark_candidates_only()
        self.assertEqual(marked, [dp])

        # 在“标记之后、移除之前”崩溃 → 重启
        store2 = PackageStore(self.root)
        report = store2.recover()
        self.assertIn(dp, report["removed"])
        self.assertNotOnDisk(dp)
        # 存活包始终不指向缺失内容
        self.assertEqual(store2.read_block(ds), shared)
        self.assertTrue(os.path.isfile(
            digest_path(self.store.objects_dir, ds)))
        for pkg in store2.list_packages():
            for digest in pkg["blocks"]:
                self.assertIsNotNone(store2.read_block(digest))
        # 再次重启是幂等的
        store3 = PackageStore(self.root)
        again = store3.recover()
        self.assertEqual(again["removed"], [])

    def test_restart_after_move_crash_converges(self):
        # 模拟“已移动到 staging、文件尚未删除、进程崩溃”
        d = sha256_text("doomed")
        self.store.put_loose_block("doomed")
        self.store.mark_candidates_only()
        obj = digest_path(self.store.objects_dir, d)
        staged = os.path.join(self.store.staging_dir, d)
        os.replace(obj, staged)
        self.assertTrue(os.path.isfile(staged))  # 移除中断
        marker = os.path.join(self.store.staging_dir, f"{CANDIDATE_MARKER}-{d}")
        self.assertTrue(os.path.isfile(marker))

        store2 = PackageStore(self.root)
        report = store2.recover()
        self.assertIn(d, report["removed"])
        self.assertFalse(os.path.exists(staged))
        self.assertFalse(os.path.exists(marker))
        self.assertIsNone(store2.read_block(d))

    def test_restart_reconciles_refs_from_manifests(self):
        # 模拟“manifest 已落盘、refs.json 未更新即崩溃”：重启后以目录为
        # 权威重算引用计数，且对账不会误删目录仍引用的对象。
        a, b = "frame-a", "shared-frame"
        da, ds = sha256_text(a), sha256_text(b)
        self.store.commit("pkg-a", block_texts=[a, b])
        self.store.commit("pkg-b", block_texts=[b])
        # 人为制造陈旧 refs（清空，仿佛第二次提交的计数未落盘）
        with open(os.path.join(self.root, "refs.json"), "w", encoding="utf-8") as fh:
            json.dump({da: 1}, fh)

        store2 = PackageStore(self.root)
        report = store2.recover()
        self.assertTrue(report["refs_reconciled"])
        refs = store2.refs_snapshot()
        self.assertEqual(refs.get(ds), 2)
        self.assertEqual(refs.get(da), 1)
        self.assertEqual(report["dangling"], [])
        # 对账后 GC 也不会动存活块
        gc = store2.garbage_collect()
        self.assertEqual(gc["removed"], [])
        self.assertEqual(store2.read_block(ds), b)

    def test_publish_never_reattaches_staging_objects(self):
        # 在 staging 放一个“删除中的旧对象”，提交引用该摘要必须失败，
        # 且该对象不得被复活到 objects/。
        d = sha256_text("zombie")
        with open(os.path.join(self.store.staging_dir, d), "w", encoding="utf-8") as fh:
            fh.write("zombie")
        from app.store import MissingBlocks
        with self.assertRaises(MissingBlocks):
            self.store.commit("pkg-a", block_digests=[d])
        self.assertFalse(os.path.isfile(digest_path(self.store.objects_dir, d)))


class HealthTests(StoreTestBase):
    def test_objects_writable_reports_true(self):
        self.assertTrue(self.store.objects_writable())

    def test_objects_writable_reports_false_when_readonly(self):
        os.chmod(self.store.objects_dir, 0o555)
        try:
            self.assertFalse(self.store.objects_writable())
        finally:
            os.chmod(self.store.objects_dir, 0o755)


if __name__ == "__main__":
    unittest.main(verbosity=2)
