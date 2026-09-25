#!/usr/bin/env python3
"""Container HEALTHCHECK probe: /health must answer 200 and report writable."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

port = int(os.environ.get("PORT", "8080"))
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
        body = json.load(resp)
    ok = resp.status == 200 and body.get("objects_dir_writable") and body.get("integrity", {}).get("ok")
except Exception:  # noqa: BLE001 - any probe failure is an unhealthy container
    ok = False

sys.exit(0 if ok else 1)
