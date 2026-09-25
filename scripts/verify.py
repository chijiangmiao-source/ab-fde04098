#!/usr/bin/env python3
"""verify 服务入口：交错完成代码测试、前后端构建与 API/HTTP 冒烟。

执行顺序（围绕三个业务验收点交错安排）：
  1. 后端代码测试（共享保留/两阶段 GC/重启收敛/修订冲突的底层不变量）
  2. 前端构建（语法门禁 + 产物打包）
  3. 后端构建（字节码编译门禁）
  4. API/HTTP 冒烟：
       - 本地子进程模式：覆盖全部三个业务场景（含 SIGKILL 故障重启）
       - 若设置 REMOTE_SMOKE_URL：再对已部署服务做远程冒烟
  完成后退出，退出码按位报告验收状态：
       bit 0 (1)  共享引用保留失败
       bit 1 (2)  故障重启收敛失败
       bit 2 (4)  修订冲突失败
       bit 3 (8)  代码测试失败
       bit 4 (16) 构建失败
       bit 5 (32) 基础设施/健康/页面失败
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL_SHARED = 1
FAIL_RESTART = 2
FAIL_CONFLICT = 4
FAIL_UNIT = 8
FAIL_BUILD = 16
FAIL_INFRA = 32


def run(label, cmd, env_extra=None):
    print(f"\n=========== {label} ===========", flush=True)
    print(f"$ {' '.join(cmd)}", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(ROOT, "backend")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    print(f"-> {label} 退出码 {proc.returncode}", flush=True)
    return proc.returncode


def main() -> int:
    failures = 0

    # 1) 后端代码测试
    rc = run("代码测试：后端 unittest",
             [sys.executable, "-m", "unittest", "discover", "-s", "backend/tests"])
    if rc != 0:
        failures |= FAIL_UNIT

    # 2) 前端构建（交错：先产出页面，再做后端编译门禁）
    rc = run("前端构建：node --check + 产物打包",
             ["node", "scripts/build_frontend.mjs"])
    backend_rc = run("后端构建：py_compile 门禁",
                     [sys.executable, "scripts/build_backend.py"])
    if rc != 0 or backend_rc != 0:
        failures |= FAIL_BUILD

    # 3) 本地 API/HTTP 冒烟（覆盖三个业务场景，含 SIGKILL 重启收敛）
    print("\n=========== API/HTTP 冒烟：本地子进程（含故障注入） ===========",
          flush=True)
    smoke_env = os.environ.copy()
    smoke_env["PYTHONPATH"] = os.path.join(ROOT, "backend")
    smoke = subprocess.run(
        [sys.executable, "scripts/smoke.py"], cwd=ROOT, env=smoke_env)
    # smoke 的退出码本身即业务位掩码（1/2/4/32）
    smoke_rc = smoke.returncode
    print(f"-> 本地冒烟退出码 {smoke_rc}", flush=True)
    failures |= smoke_rc & (FAIL_SHARED | FAIL_RESTART | FAIL_CONFLICT | FAIL_INFRA)

    # 4) 对已部署服务的远程冒烟（Compose verify 中由环境变量给出）
    remote_url = os.environ.get("REMOTE_SMOKE_URL")
    if remote_url:
        print(f"\n=========== API/HTTP 冒烟：已部署服务 {remote_url} ===========",
              flush=True)
        remote = subprocess.run(
            [sys.executable, "scripts/smoke.py", "--url", remote_url], cwd=ROOT)
        print(f"-> 远程冒烟退出码 {remote.returncode}", flush=True)
        failures |= remote.returncode & (FAIL_SHARED | FAIL_CONFLICT | FAIL_INFRA)

    print("\n================ verify 验收总览 ================")
    rows = [
        (FAIL_SHARED, "共享引用保留（撤下一包后另一包可读；末引用撤下后回收）"),
        (FAIL_RESTART, "故障重启收敛（标记/移除中断后重启，无悬空引用）"),
        (FAIL_CONFLICT, "修订冲突（过期修订稳定拒绝且无副作用）"),
        (FAIL_UNIT, "后端代码测试"),
        (FAIL_BUILD, "前后端构建"),
        (FAIL_INFRA, "基础设施（健康接口对象目录可写 / 页面 HTTP）"),
    ]
    for bit, label in rows:
        print(f"  [{'PASS' if not failures & bit else 'FAIL'}] {label}")
    print(f"\nverify 退出码: {failures}（0 = 全部验收通过）")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
