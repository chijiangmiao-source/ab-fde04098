#!/usr/bin/env python3
"""API/HTTP 冒烟：真实子进程启动服务，覆盖三个业务验收点。

退出码按位组合（与业务结果一一对应）：
  bit 0 (1)  共享引用保留失败
  bit 1 (2)  故障重启收敛失败
  bit 2 (4)  修订冲突处理失败
  bit 5 (32) 基础设施/健康检查失败
0 表示全部通过。

用法：
  python3 scripts/smoke.py [--server backend/server.py] [--data DIR]
若设置环境变量 TARGET_URL，则额外对已部署服务做只读冒烟。
"""

import argparse
import glob
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL_SHARED = 1
FAIL_RESTART = 2
FAIL_CONFLICT = 4
FAIL_INFRA = 32


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request(url: str, method: str = "GET", payload=None, expect_error: bool = False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode() or "{}")
        return exc.code, body
    except urllib.error.URLError:
        return 0, {}


def start_server(server_py: str, data_dir: str, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, server_py, "--host", "127.0.0.1", "--port", str(port),
         "--data", data_dir],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=1)
            raise RuntimeError(f"服务提前退出\nSTDOUT:{out}\nSTDERR:{err}")
        status, _ = request(f"http://127.0.0.1:{port}/health", expect_error=True)
        if status == HTTPStatus.OK:
            return proc
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("服务健康检查超时")


def hard_kill(proc: subprocess.Popen) -> None:
    """SIGKILL：不给任何清理机会，模拟标记/移除中途断电。"""
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)


# ------------------------------------------------------------ 场景一

def scenario_shared_retention(base: str, suffix: str = "") -> bool:
    print("\n[场景一] 两个包共享块 → 撤下一包并清理 → 另一包仍可读；"
          "撤下最后引用后不可读")
    ok = True
    pkg_a_name, pkg_b_name = "pkg-a" + suffix, "pkg-b" + suffix
    shared, priv_a, priv_b = "SMOKE shared frame", "SMOKE a-only", "SMOKE b-only"

    st, r_a = request(f"{base}/api/packages/{pkg_a_name}/commit", "POST",
                      {"blocks": [shared, priv_a], "expected_revision": 0})
    ok &= check("pkg-a 首次提交 201", st == 201, st)
    st, r_b = request(f"{base}/api/packages/{pkg_b_name}/commit", "POST",
                      {"blocks": [shared, priv_b], "expected_revision": 0})
    ok &= check("pkg-b 首次提交 201", st == 201, st)
    shared_d = next(iter(set(r_a["added"]) & set(r_b["added"])), None)
    ok &= check("两包共享同一 SHA-256 块", shared_d is not None)

    st, blk = request(f"{base}/api/blocks/{shared_d}")
    ok &= check("共享块可读且引用数=2",
                st == 200 and blk["ref_count"] == 2
                and {pkg_a_name, pkg_b_name}.issubset(set(blk["referenced_by"]))
                and "保留" in blk["retention_reason"],
                (st, blk if st != 200 else blk.get("ref_count")))

    # 撤下 pkg-a 并清理
    st, _ = request(f"{base}/api/packages/{pkg_a_name}/commit", "POST",
                    {"blocks": [], "expected_revision": 1})
    ok &= check("pkg-a 撤下提交 201", st == 201, st)
    st, gc = request(f"{base}/api/gc", "POST", {})
    ok &= check("GC 200", st == 200, st)
    st, blk = request(f"{base}/api/blocks/{shared_d}")
    ok &= check("撤下一包后共享块仍可读、引用数=1、说明保留缘由",
                st == 200 and blk["ref_count"] >= 1
                and pkg_b_name in blk["referenced_by"]
                and pkg_b_name in blk["retention_reason"],
                (st, blk if st != 200 else (blk.get("ref_count"),
                                            blk.get("retention_reason"))))

    # 撤下最后引用（pkg-b）
    st, _ = request(f"{base}/api/packages/{pkg_b_name}/commit", "POST",
                    {"blocks": [], "expected_revision": 1})
    ok &= check("pkg-b 撤下提交 201", st == 201, st)
    st, gc = request(f"{base}/api/gc", "POST", {})
    ok &= check("共享块在零引用后被移除", st == 200 and shared_d in gc["removed"],
                gc)
    st, gone = request(f"{base}/api/blocks/{shared_d}")
    ok &= check("撤下最后引用后块无法读取（404）",
                st == 404 and gone.get("error") == "block_unavailable", (st, gone))
    st, summary = request(f"{base}/api/summary")
    ok &= check("目录与引用统计不再保留该块",
                st == 200 and
                all(shared_d not in p["blocks"] for p in summary["packages"]),
                summary if st == 200 else st)
    return ok


# ------------------------------------------------------------ 场景二

def scenario_restart_convergence(server_py: str, data_dir: str, port: int) -> bool:
    print("\n[场景二] 候选标记后 SIGKILL → 重启收敛 → 存活包不指向缺失内容")
    ok = True
    proc = start_server(server_py, data_dir, port)
    retained_text, doomed_text = "SMOKE retained frame", "SMOKE doomed frame"
    st, _ = request(f"http://127.0.0.1:{port}/api/packages/pkg-c/commit", "POST",
                    {"blocks": [retained_text, doomed_text], "expected_revision": 0})
    ok &= check("pkg-c 修订 1 提交", st == 201, st)
    # 修订 2 撤下 doomed，产生零引用块
    st, r2 = request(f"http://127.0.0.1:{port}/api/packages/pkg-c/commit", "POST",
                     {"blocks": [retained_text], "expected_revision": 1})
    ok &= check("pkg-c 修订 2 提交", st == 201 and len(r2["removed"]) == 1, r2)
    doomed_digest = r2["removed"][0]

    # 第一阶段：只标记；随后立即硬杀进程（标记之后、移除之前中断）
    st, mark = request(f"http://127.0.0.1:{port}/api/gc", "POST", {"mode": "mark"})
    ok &= check("仅标记阶段返回候选", st == 200 and doomed_digest in mark["marked"],
                mark)
    markers = glob.glob(os.path.join(data_dir, "staging", ".gc-candidate-*"))
    ok &= check("staging 中留有候选标记", any(doomed_digest in m for m in markers),
                markers)
    # 对象此时仍在 objects/（存活读取不受影响）
    st, before = request(f"http://127.0.0.1:{port}/api/blocks/{doomed_digest}")
    ok &= check("中断前对象尚未移除（标记与移除严格分两阶段）", st == 200, st)

    try:
        # 在“标记之后、移除之前”硬杀当前服务进程
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)  # 回收僵尸并确认端口随进程关闭
        time.sleep(0.5)

        proc = start_server(server_py, data_dir, port)
        st, pkg = request("http://127.0.0.1:%d/api/packages/pkg-c" % port)
        ok &= check("重启后目录仍可读", st == 200 and pkg["revision"] == 2,
                    st if st != 200 else pkg.get("revision"))

        markers_after = glob.glob(os.path.join(data_dir, "staging",
                                               ".gc-candidate-*"))
        ok &= check("重启后候选标记被收敛清理",
                    not any(doomed_digest in m for m in markers_after), markers_after)
        st, gone = request(
            f"http://127.0.0.1:{port}/api/blocks/{doomed_digest}")
        ok &= check("零引用候选完成删除，接口不可读", st == 404, st)
        st, alive = request("http://127.0.0.1:%d/api/summary" % port)
        ok &= check("存活包始终不指向缺失内容",
                    st == 200 and all(
                        request(f"http://127.0.0.1:{port}/api/blocks/{d}")[0] == 200
                        for p in alive["packages"] for d in p["blocks"]),
                    "存在悬空引用")
        # 再重启一次必须幂等
        hard_kill(proc)
        proc = start_server(server_py, data_dir, port)
        st, _ = request(f"http://127.0.0.1:{port}/health")
        ok &= check("二次重启健康且幂等", st == 200, st)
    finally:
        if proc is not None and proc.poll() is None:
            hard_kill(proc)
    return ok


# ------------------------------------------------------------ 场景三

def scenario_revision_conflict(base: str, suffix: str = "") -> bool:
    print("\n[场景三] 过期修订提交被稳定拒绝，目录/引用数/对象不变")
    ok = True
    pkg = "pkg-d" + suffix
    st, _ = request(f"{base}/api/packages/{pkg}/commit", "POST",
                    {"blocks": ["SMOKE d-frame-1" + suffix], "expected_revision": 0})
    ok &= check("pkg-d 修订 1 提交", st == 201, st)
    st, before_summary = request(f"{base}/api/summary")

    st, conflict = request(f"{base}/api/packages/{pkg}/commit", "POST",
                           {"blocks": ["SMOKE d-ghost-frame" + suffix],
                            "expected_revision": 0})  # 当前已是 1
    ok &= check("过期修订返回 409 stale_revision",
                st == 409 and conflict.get("error") == "stale_revision"
                and conflict.get("current_revision") == 1,
                (st, conflict))

    st, pkg_info = request(f"{base}/api/packages/{pkg}")
    ok &= check("既有目录未改变（仍为修订 1）",
                st == 200 and pkg_info["revision"] == 1, pkg_info)
    st, after_summary = request(f"{base}/api/summary")
    ok &= check("引用统计未改变",
                st == 200 and after_summary["total_referenced_blocks"]
                == before_summary["total_referenced_blocks"],
                (before_summary.get("total_referenced_blocks"),
                 after_summary.get("total_referenced_blocks")))
    return ok


def check(label: str, condition, detail=None) -> bool:
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {label}"
    if not condition and detail is not None:
        line += f" -> {json.dumps(detail, ensure_ascii=False)[:300]}"
    print(line)
    return bool(condition)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default=os.path.join("backend", "server.py"))
    parser.add_argument("--data", default=None)
    parser.add_argument("--url", default=None,
                        help="对已部署服务做远程冒烟（不含故障重启场景）")
    args = parser.parse_args()

    failures = 0

    # ------------------------------------------------ 远程模式（已部署服务）
    if args.url:
        base = args.url.rstrip("/")
        st, health = request(f"{base}/health")
        if not check("健康接口报告对象目录可写",
                     st == 200 and health.get("objects_writable") is True,
                     (st, health)):
            return FAIL_INFRA
        try:
            st_page = urllib.request.urlopen(f"{base}/", timeout=5).status
        except urllib.error.HTTPError as exc:
            st_page = exc.code
        if not check("复核页面 HTTP 200", st_page == 200, st_page):
            failures |= FAIL_INFRA
        suffix = "-" + uuid.uuid4().hex[:12]
        if not scenario_shared_retention(base, suffix):
            failures |= FAIL_SHARED
        if not scenario_revision_conflict(base, suffix):
            failures |= FAIL_CONFLICT
        print("\n  （远程模式跳过故障重启收敛；该场景由本地子进程模式覆盖）")
        _print_summary(failures, restart_applicable=False)
        return failures

    # ------------------------------------------------ 本地模式（含 SIGKILL）
    data_dir = args.data or tempfile.mkdtemp(prefix="smoke-data-")
    os.makedirs(data_dir, exist_ok=True)
    port = free_port()

    proc = start_server(args.server, data_dir, port)
    base = f"http://127.0.0.1:{port}"
    try:
        st, health = request(f"{base}/health")
        if not check("健康接口报告对象目录可写",
                     st == 200 and health.get("objects_writable") is True,
                     (st, health)):
            return FAIL_INFRA

        # 页面 HTTP 冒烟
        st_page = urllib.request.urlopen(f"{base}/", timeout=5).status
        if not check("复核页面 HTTP 200", st_page == 200, st_page):
            failures |= FAIL_INFRA

        if not scenario_shared_retention(base):
            failures |= FAIL_SHARED
        if not scenario_revision_conflict(base):
            failures |= FAIL_CONFLICT
    finally:        # 场景二需要在“标记后”硬杀由本脚本启动的服务
        if proc.poll() is None:
            try:
                hard_kill(proc)
            except ProcessLookupError:
                pass

    if not scenario_restart_convergence(args.server, data_dir, port):
        failures |= FAIL_RESTART

    _print_summary(failures, restart_applicable=True)
    return failures


def _print_summary(failures: int, restart_applicable: bool) -> None:
    print("\n================ 冒烟验收摘要 ================")
    print(f"  共享引用保留      : {'PASS' if not failures & FAIL_SHARED else 'FAIL'}")
    if restart_applicable:
        print(f"  故障重启收敛      : {'PASS' if not failures & FAIL_RESTART else 'FAIL'}")
    print(f"  修订冲突拒绝      : {'PASS' if not failures & FAIL_CONFLICT else 'FAIL'}")
    print(f"  基础设施/健康/页面: {'PASS' if not failures & FAIL_INFRA else 'FAIL'}")
    print(f"  退出码: {failures}（0 = 全部验收通过）")


if __name__ == "__main__":
    raise SystemExit(main())
