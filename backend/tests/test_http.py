"""HTTP 冒烟/接口测试：在真实端口启动服务，端到端验证验收语义。"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import build_server  # noqa: E402


def _wait_port(host, port, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=1)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return
        except OSError as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"server not ready: {last}")


class HttpTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="calhttp-")
        self.server = build_server(self.root, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _wait_port("127.0.0.1", self.port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None
                     else None, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw


class ApiAcceptanceTests(HttpTestBase):
    def test_full_acceptance_flow(self):
        # 健康：对象目录可写
        status, health = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["objects_writable"])

        shared, priv_a, priv_b = "FRAME=shared-9101", "FRAME=a-only", "FRAME=b-only"
        s1, b1 = self.req("POST", "/api/packages/pkg-a/commit",
                          {"blocks": [shared, priv_a], "expected_revision": 0})
        self.assertEqual(s1, 201, b1)
        s2, b2 = self.req("POST", "/api/packages/pkg-b/commit",
                          {"blocks": [shared, priv_b], "expected_revision": 0})
        self.assertEqual(s2, 201, b2)
        shared_digest = b1["added"][0] if b1["added"][0] in b2["added"] \
            else next(d for d in b2["added"] if d in b1["added"])

        # 共享块接口可读，且页面说明保留缘由
        st, blk = self.req("GET", f"/api/blocks/{shared_digest}")
        self.assertEqual(st, 200)
        self.assertEqual(blk["ref_count"], 2)
        self.assertEqual(set(blk["referenced_by"]), {"pkg-a", "pkg-b"})
        self.assertIn("保留", blk["retention_reason"])

        # 摘要/修订/引用数
        st, summary = self.req("GET", "/api/summary")
        self.assertEqual(st, 200)
        self.assertEqual(summary["total_packages"], 2)
        for pkg in summary["packages"]:
            self.assertGreaterEqual(pkg["revision"], 1)

        # 过期修订：稳定 409 拒绝（包当前为修订 1，却基于修订 0 提交），
        # 不改变目录/引用/对象
        st, conflict = self.req(
            "POST", "/api/packages/pkg-a/commit",
            {"blocks": [shared, priv_a, "brand-new-ghost"], "expected_revision": 0})
        self.assertEqual(st, 409)
        self.assertEqual(conflict["error"], "stale_revision")
        self.assertEqual(conflict["current_revision"], 1)
        st, pkg_a = self.req("GET", "/api/packages/pkg-a")
        self.assertEqual(pkg_a["revision"], 1)
        self.assertEqual(len(pkg_a["blocks"]), 2)
        st, blk2 = self.req("GET", f"/api/blocks/{shared_digest}")
        self.assertEqual(blk2["ref_count"], 2)

        # 撤下 pkg-a 并清理：共享块保留，私有块回收
        st, rem = self.req("POST", "/api/packages/pkg-a/commit",
                           {"blocks": [], "expected_revision": 1})
        self.assertEqual(st, 201)
        st, gc1 = self.req("POST", "/api/gc")
        self.assertEqual(st, 200)
        st, still = self.req("GET", f"/api/blocks/{shared_digest}")
        self.assertEqual(st, 200)  # 另一包仍能读取
        self.assertEqual(still["ref_count"], 1)
        self.assertIn("pkg-b", still["retention_reason"])
        st, summary = self.req("GET", "/api/summary")
        a_pkg = next(p for p in summary["packages"] if p["package"] == "pkg-a")
        self.assertEqual(a_pkg["blocks"], [])
        b_pkg = next(p for p in summary["packages"] if p["package"] == "pkg-b")
        self.assertIn(shared_digest, b_pkg["blocks"])

        # 撤下最后引用（pkg-b），回收后不可读，目录与引用统计不再保留
        self.req("POST", "/api/packages/pkg-b/commit",
                 {"blocks": [], "expected_revision": 1})
        st, gc2 = self.req("POST", "/api/gc")
        self.assertIn(shared_digest, gc2["removed"])
        st, gone = self.req("GET", f"/api/blocks/{shared_digest}")
        self.assertEqual(st, 404)
        self.assertEqual(gone["error"], "block_unavailable")
        st, summary = self.req("GET", "/api/summary")
        self.assertEqual(summary["total_referenced_blocks"], 0)
        for pkg in summary["packages"]:
            self.assertNotIn(shared_digest, pkg["blocks"])

    def test_digest_endpoint_matches_sha256(self):
        import hashlib
        text = "CAL-FRAME digest probe"
        st, body = self.req("POST", "/api/digest", {"text": text})
        self.assertEqual(st, 200)
        self.assertEqual(body["digest"],
                         hashlib.sha256(text.encode()).hexdigest())

    def test_static_page_served(self):
        for path, needle in (("/", b"<title>"), ("/static/app.js", b"refreshHealth"),
                             ("/static/styles.css", b".panel")):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200, path)
            self.assertIn(needle, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
