#!/usr/bin/env python3
"""End-to-end HTTP smoke test for the calibration-block service.

It spawns the real Flask server as a subprocess, so crash/restart convergence
is tested by actually killing and relaunching the process over the same data
directory. Uses only the Python standard library.

Business scenarios (interleaved with build/test by scripts/verify.sh):

  A. shared-reference retention  - unpublish one package, shared block stays
     readable with an explained retention reason; after the LAST reference is
     removed the block is unreadable and vanishes from stats.
  B. crash-restart convergence   - torn mark/sweep state converges on reboot,
     live packages never point at missing content.
  C. revision conflicts          - stale submissions are rejected (409) without
     touching directories, refcounts or objects, then retry on current rev.

Exit code 0 only if every acceptance check passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

HOST = os.environ.get("SMOKE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMOKE_PORT", "8099"))
BASE = f"http://{HOST}:{PORT}"

failures: list[str] = []
checks = 0


def check(cond: bool, label: str) -> bool:
    global checks
    checks += 1
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}")
    if not cond:
        failures.append(label)
    return cond


def http(method: str, path: str, body=None, raw: bytes | None = None, expect=None):
    url = BASE + path
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = "text/plain; charset=utf-8"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    parsed = None
    if payload:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = payload
    if expect is not None and status != expect:
        raise AssertionError(f"{method} {path} -> {status}, expected {expect}: {payload[:300]!r}")
    return status, parsed, payload


def wait_health(proc, timeout=15.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            status, body, _ = http("GET", "/health")
            if status == 200:
                return body
        except OSError as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"server did not become healthy: {last}")


class Server:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.proc: subprocess.Popen | None = None

    def start(self):
        env = os.environ.copy()
        env.update({
            "DATA_DIR": self.data_dir,
            "PORT": str(PORT),
            "HOST": HOST,
            "PYTHONPATH": str(BACKEND_DIR),
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.wsgi"],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        wait_health(self.proc)
        return self

    def stop(self):
        assert self.proc
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    def restart(self):
        print("  --- restarting server process (crash-recovery convergence) ---")
        self.stop()
        self.start()


def upload(text: str) -> str:
    status, body, _ = http("POST", "/api/blocks", raw=text.encode(), expect=201)
    assert body["sha"] == hashlib.sha256(text.encode()).hexdigest()
    return body["sha"]


def submit(directory: str, base_rev: int, changes, note="", expect=None):
    return http("POST", "/api/submit",
                {"directory": directory, "base_rev": base_rev, "changes": changes, "note": note},
                expect=expect)


# ---------------------------------------------------------------- scenarios
def scenario_http_basics():
    print("\n=== HTTP 冒烟：页面与静态资源 ===")
    status, _, payload = http("GET", "/", expect=200)
    check(b"<!doctype html>" in payload.lower() and "校准" in payload.decode(), "GET / 返回评审页面 HTML")
    check(http("GET", "/app.js", expect=200)[0] == 200, "GET /app.js 返回前端脚本")
    check(http("GET", "/app.css", expect=200)[0] == 200, "GET /app.css 返回样式")
    h = http("GET", "/health", expect=200)[1]
    check(h["objects_dir_writable"] is True and h["integrity"]["ok"] is True,
          "健康接口报告对象目录可写且完整性正常")


def scenario_shared_reference(server: Server):
    print("\n=== 场景 A：共享引用保留 → 撤下一包并清理，另一包仍可读 ===")
    shared = upload("dark-frame-shared-text-block")
    a_only = upload("package-a-only-flat-field")
    b_only = upload("package-b-only-geometry")

    submit("pkg-a", 0, [{"op": "add", "sha": shared, "name": "dark"},
                        {"op": "add", "sha": a_only, "name": "flat-a"}], expect=201)
    submit("pkg-b", 1, [{"op": "add", "sha": shared, "name": "dark"},
                        {"op": "add", "sha": b_only, "name": "geo-b"}], expect=201)
    stats = http("GET", "/api/stats", expect=200)[1]
    check(stats["refcount"][shared] == 2, "共享块引用计数为 2")

    # Reviewer takes package A offline, then runs cleanup.
    pkg_a = http("GET", "/api/directories/pkg-a", expect=200)[1]
    changes = [{"op": "remove", "sha": e["sha"]} for e in pkg_a["entries"]]
    submit("pkg-a", 2, changes, note="reviewer unpublishes pkg-a", expect=201)
    gc = http("POST", "/api/gc", {"mode": "auto"}, expect=200)[1]
    check(a_only in gc["removed"] and shared not in gc["removed"],
          "清理只回收 pkg-a 独占块，共享块未被移除")

    status, _, payload = http("GET", f"/api/blocks/{shared}", expect=200)
    check(payload == b"dark-frame-shared-text-block", "另一包仍可通过接口读取共享块")
    detail = http("GET", f"/api/blocks/{shared}/status", expect=200)[1]
    check(detail["readable"] is True and detail["state"] == "retained"
          and detail["referrers"] == ["pkg-b"] and "pkg-b" in detail["reason"],
          f"页面可展示保留缘由：{detail['reason']}")

    # Remove the LAST reference: block must become unreadable and disappear
    # from directory edges and reference stats.
    pkg_b = http("GET", "/api/directories/pkg-b", expect=200)[1]
    cur = http("GET", "/api/stats", expect=200)[1]["rev"]
    changes = [{"op": "remove", "sha": e["sha"]} for e in pkg_b["entries"]]
    submit("pkg-b", cur, changes, expect=201)
    http("POST", "/api/gc", {"mode": "auto"}, expect=200)
    check(http("GET", f"/api/blocks/{shared}")[0] == 404, "撤下最后引用后该块无法读取 (404)")
    final_status = http("GET", f"/api/blocks/{shared}/status", expect=200)[1]
    check(final_status["state"] == "missing" and final_status["refcount"] == 0,
          "状态接口说明块已随最后引用回收")
    stats = http("GET", "/api/stats", expect=200)[1]
    check(shared not in stats["refcount"] and b_only not in stats["refcount"]
          and stats["objects"] == 0 and stats["candidates"] == [],
          "目录引用统计中不再保留该块，对象数归零")
    pkg_b_after = http("GET", "/api/directories/pkg-b", expect=200)[1]
    check(pkg_b_after["entries"] == [], "存活目录边不再列出该块")
    check(http("GET", "/health", expect=200)[1]["integrity"]["ok"] is True,
          "完整性检查通过：存活包不指向缺失内容")


def scenario_crash_restart(server: Server):
    print("\n=== 场景 B：标记/移除中断后重启收敛 ===")
    objects = Path(server.data_dir) / "objects"

    # B1: orphan marked (marker present, bytes present), sweep interrupted.
    after_mark = upload("orphan-marked-then-crash")
    http("POST", "/api/gc", {"mode": "mark"}, expect=200)
    check((objects / f"{after_mark}.cand").exists(), "中断前已留下候选标记")
    server.restart()  # boot recovery must finish the zero-ref removal
    check(http("GET", f"/api/blocks/{after_mark}")[0] == 404,
          "重启后：标记后中断的零引用块被收敛移除")
    stats = http("GET", "/api/stats", expect=200)[1]
    check(stats["candidates"] == [] and after_mark not in stats["refcount"],
          "重启后：候选标记被清理，引用统计无残留")

    # B2: torn removal - bytes already gone, marker left behind.
    torn = upload("orphan-bytes-unlinked-marker-left")
    (objects / f"{torn}.cand").write_text(
        json.dumps({"sha": torn, "reason": "simulated crash mid-removal"}))
    (objects / torn).unlink()
    server.restart()
    check(http("GET", f"/api/blocks/{torn}")[0] == 404, "重启后：撕裂移除的块仍不可读")
    check(not (objects / f"{torn}.cand").exists(), "重启后：遗留标记被收敛清除")

    # B3: live package with a stale/rogue marker must keep its block.
    live = upload("live-package-block-with-rogue-marker")
    cur = http("GET", "/api/stats", expect=200)[1]["rev"]
    submit("pkg-live", cur, [{"op": "add", "sha": live, "name": "live"}], expect=201)
    (objects / f"{live}.cand").write_text(json.dumps({"sha": live, "reason": "rogue"}))
    server.restart()
    status, _, payload = http("GET", f"/api/blocks/{live}", expect=200)
    check(payload == b"live-package-block-with-rogue-marker",
          "重启后：存活包引用的块仍可读取（标记被释放而非移除）")
    check(not (objects / f"{live}.cand").exists(), "重启后：存活块的错误标记被释放")
    check(http("GET", "/health", expect=200)[1]["integrity"]["ok"] is True,
          "重启后：完整性 OK，存活包绝不指向缺失内容")


def scenario_revision_conflict():
    print("\n=== 场景 C：过期修订稳定拒绝且不改变既有状态 ===")
    x = upload("revision-conflict-probe")
    stats_before = http("GET", "/api/stats", expect=200)[1]
    dirs_before = http("GET", "/api/directories", expect=200)[1]
    revs_before = http("GET", "/api/revisions", expect=200)[1]

    stale = max(0, stats_before["rev"] - 1)
    status, body, _ = submit("pkg-conflict", stale,
                             [{"op": "add", "sha": x, "name": "late"}], expect=409)
    check(body["code"] == "revision_conflict" and body["current_rev"] == stats_before["rev"],
          f"过期修订被稳定拒绝 (409 revision_conflict, current=r{body['current_rev']})")

    stats_after = http("GET", "/api/stats", expect=200)[1]
    dirs_after = http("GET", "/api/directories", expect=200)[1]
    revs_after = http("GET", "/api/revisions", expect=200)[1]
    check(stats_after == stats_before, "拒绝后引用计数/对象统计/候选列表完全不变")
    check(dirs_after == dirs_before, "拒绝后目录边完全不变")
    check(revs_after == revs_before, "拒绝后修订历史不新增条目")
    check(http("GET", f"/api/blocks/{x}", expect=200)[0] == 200,
          "被拒提交涉及的对象文件原样保留")

    # Retry against the current revision succeeds.
    status, body, _ = submit("pkg-conflict", stats_before["rev"],
                             [{"op": "add", "sha": x, "name": "on-time"}], expect=201)
    check(body["rev"] == stats_before["rev"] + 1 and body["summary"]["added"] == 1,
          f"按当前修订重新提交成功（新 r{body['rev']}）")


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="calblock-smoke-")
    print(f"smoke data dir: {data_dir}")
    server = Server(data_dir)
    try:
        server.start()
        scenario_http_basics()
        scenario_shared_reference(server)
        scenario_crash_restart(server)
        scenario_revision_conflict()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"scenario error: {exc!r}")
        if server.proc is not None:
            out = server.proc.stdout.read() if server.proc.stdout else b""
            print("\n--- server output ---\n" + out.decode(errors="replace")[-2000:])
    finally:
        server.stop()
        shutil.rmtree(data_dir, ignore_errors=True)

    print("\n================ 验收结果 ================")
    print(f"checks: {checks}, passed: {checks - len(failures)}, failed: {len(failures)}")
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        print("验收状态：未通过 (exit 1)")
        return 1
    print("共享引用保留 / 故障重启收敛 / 修订冲突：全部通过")
    print("验收状态：通过 (exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
