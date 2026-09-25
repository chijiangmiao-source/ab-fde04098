#!/usr/bin/env python3
"""后端构建门禁：编译全部 Python 源（py_compile），零第三方依赖。"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rc = 0
for path in sorted(ROOT.glob("backend/**/*.py")):
    try:
        py_compile.compile(str(path), doraise=True)
        print(f"[build-backend] ok {path.relative_to(ROOT)}")
    except py_compile.PyCompileError as exc:
        print(exc)
        rc = 1
sys.exit(rc)
