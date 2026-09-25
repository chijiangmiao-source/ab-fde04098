#!/usr/bin/env bash
# Acceptance gate: code tests + frontend/backend builds + API/HTTP smoke,
# interleaved around the three business outcomes:
#   1) shared-reference retention
#   2) crash-restart convergence
#   3) revision conflicts
#
# Exits 0 only if every stage passes; the exit code is the acceptance status.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.venv"
SMOKE_PORT="${SMOKE_PORT:-8099}"
export SMOKE_PORT
export PYTHONPATH="$ROOT/backend"

stage() { printf '\n================ %s ================\n' "$1"; }
fail() { printf '\n验收未通过：%s (exit 1)\n' "$1" >&2; exit 1; }

# ------------------------------------------------------------- python setup
stage "0/4 环境准备"
if ! "$PYTHON" -c "import flask, pytest" >/dev/null 2>&1; then
  echo ">> 创建虚拟环境并安装后端依赖"
  "$PYTHON" -m venv "$VENV" || fail "无法创建 venv"
  # PEP 668 systems require --break-system-packages outside a venv; inside a
  # venv that flag is harmless, so only add it when installing system-wide.
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r backend/requirements.txt || fail "后端依赖安装失败"
  PYTHON="$VENV/bin/python"
elif [ -x "$VENV/bin/python" ]; then
  PYTHON="$VENV/bin/python"
fi
echo "python: $($PYTHON --version 2>&1)"

# ------------------------------------------------- interleave: backend tests
stage "1/4 后端代码测试（存储一致性 / 共享引用 / 修订冲突 / 重启恢复）"
( cd backend && PYTHONPATH=. "$PYTHON" -m pytest tests -q ) || fail "后端单元测试失败"

# ------------------------------------------- interleave: frontend build next
stage "2/4 前端构建（类型检查 + esbuild 打包）"
(
  cd frontend
  export npm_config_cache="$ROOT/.npm-cache"
  [ -d node_modules ] || npm install --no-audit --no-fund || exit 1
  npx tsc --noEmit || exit 1
  npm run build || exit 1
  test -s static/app.js || { echo "缺少构建产物 static/app.js"; exit 1; }
) || fail "前端构建失败"

# ------------------------------------------------- interleave: backend build
stage "3/4 后端构建（字节码编译 + 依赖可导入校验）"
"$PYTHON" -m compileall -q backend/app || fail "后端编译失败"
DATA_DIR="$(mktemp -d)" "$PYTHON" -c \
  "import sys, os; sys.path.insert(0, 'backend'); import app.api, app.wsgi; assert app.wsgi.app is not None; print('backend import ok (DATA_DIR=%s)' % os.environ['DATA_DIR'])" \
  || fail "后端模块导入失败"

# ------------------------------------------------------------- HTTP smoke
stage "4/4 API/HTTP 冒烟（真实进程 + 杀进程重启，覆盖三大业务结果）"
"$PYTHON" scripts/smoke.py || fail "API/HTTP 冒烟失败"

printf '\n全部阶段通过：代码测试、前后端构建、API/HTTP 冒烟（共享引用保留 / 故障重启收敛 / 修订冲突）\n'
printf '验收状态：通过 (exit 0)\n'
