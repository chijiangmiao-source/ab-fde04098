"""HTTP 接口（仅标准库）。

路由：
  GET  /health                         健康检查（含 objects_writable）
  GET  /api/summary                    包摘要、修订与引用数
  GET  /api/packages/<name>            单个包目录
  POST /api/packages/<name>/commit     提交新修订（expected_revision 乐观并发）
  GET  /api/blocks/<sha256>            读取块（含引用计数与保留缘由）
  POST /api/gc                         两阶段垃圾回收
  POST /api/recover                    手动收敛中断的候选标记
  GET  /                               复核员页面（静态资源 /static/*）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import (  # noqa: E402
    MissingBlocks,
    PackageStore,
    StaleRevision,
)

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BACKEND_DIR, "static")
# 未执行前端构建（开发态）时回退到源码目录。
STATIC_FALLBACK_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend", "src")

STORE_LOCK = threading.Lock()  # 串行化写操作（存储内部还有文件锁，双保险）


class Handler(BaseHTTPRequestHandler):
    server_version = "CalibrationStore/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 工具

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = HTTPStatus.OK,
              content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(data, dict):
            self._json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return None
        return data

    def log_message(self, fmt, *args):  # 简洁日志
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    @property
    def store(self) -> PackageStore:
        return self.server.store  # type: ignore[attr-defined]

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            return self._handle_health()
        if path == "/api/summary":
            return self._json(self.store.summary())
        if path.startswith("/api/packages/"):
            name = path.rsplit("/", 1)[-1]
            pkg = self.store.get_package(name)
            if pkg is None:
                return self._json({"error": "package_not_found", "package": name},
                                  HTTPStatus.NOT_FOUND)
            return self._json(pkg)
        if path.startswith("/api/blocks/"):
            return self._handle_get_block(path.rsplit("/", 1)[-1])
        if path == "/" or path == "/index.html":
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            ctype = "application/javascript; charset=utf-8" if name.endswith(".js") \
                else "text/css; charset=utf-8" if name.endswith(".css") \
                else "application/octet-stream"
            return self._serve_static(name, ctype)
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.endswith("/commit") and path.startswith("/api/packages/"):
            parts = path.strip("/").split("/")
            # api / packages / <name> / commit
            if len(parts) != 4:
                return self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return self._handle_commit(parts[2])
        if path == "/api/digest":
            return self._handle_digest()
        if path == "/api/gc":
            # 可选 body {"mode": "mark"}：只做第一阶段候选标记，供故障注入。
            length = int(self.headers.get("Content-Length") or 0)
            mode = "full"
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(payload, dict) and payload.get("mode") in ("full", "mark"):
                        mode = payload["mode"]
                except (ValueError, UnicodeDecodeError):
                    return self._json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            if mode == "mark":
                return self._json({"mode": "mark",
                                   "marked": self.store.mark_candidates_only()})
            return self._json(self.store.garbage_collect())
        if path == "/api/recover":
            return self._json(self.store.recover())
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------ 处理器

    def _handle_digest(self) -> None:
        # 供非安全上下文（无 crypto.subtle）的浏览器计算 SHA-256。
        data = self._read_json_body()
        if data is None:
            return
        text = data.get("text")
        if not isinstance(text, str):
            return self._json({"error": "text_must_be_string"}, HTTPStatus.BAD_REQUEST)
        from app.store import sha256_text
        return self._json({"digest": sha256_text(text)})

    def _handle_health(self) -> None:
        stats = self.store.storage_stats()
        writable = self.store.objects_writable()
        self._json({
            "status": "ok" if writable else "readonly",
            "objects_writable": writable,
            "storage": stats,
        }, HTTPStatus.OK if writable else HTTPStatus.SERVICE_UNAVAILABLE)

    def _handle_get_block(self, digest: str) -> None:
        content = self.store.read_block(digest)
        if content is None:
            # 撤下最后引用并回收后：接口明确不可读。
            return self._json({
                "error": "block_unavailable",
                "digest": digest,
                "reason": "该块无任何存活包引用，已被回收或从未发布。",
            }, HTTPStatus.NOT_FOUND)
        refs = self.store.ref_count(digest)
        referenced_by = [
            p["package"] for p in self.store.summary()["packages"]
            if digest.lower() in [b.lower() for b in p["blocks"]]
        ]
        self._json({
            "digest": digest.lower(),
            "content": content,
            "ref_count": refs,
            "referenced_by": referenced_by,
            "retention_reason": (
                "仍有 %d 个存活包目录引用该块（%s），故在其它包撤下后保留。"
                % (refs, "、".join(referenced_by) or "-")
            ) if refs > 0 else "当前无引用，属于可回收的游离对象。",
        })

    def _handle_commit(self, name: str) -> None:
        data = self._read_json_body()
        if data is None:
            return
        texts = data.get("blocks") or data.get("texts") or []
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return self._json({"error": "blocks_must_be_string_array"},
                              HTTPStatus.BAD_REQUEST)
        digests = data.get("block_digests") or []
        if not isinstance(digests, list) or not all(isinstance(d, str) for d in digests):
            return self._json({"error": "block_digests_must_be_string_array"},
                              HTTPStatus.BAD_REQUEST)
        try:
            self._package_name_ok(name)
        except ValueError as exc:
            return self._json({"error": "invalid_package", "detail": str(exc)},
                              HTTPStatus.BAD_REQUEST)
        expected = data.get("expected_revision")
        if expected is not None and not isinstance(expected, int):
            return self._json({"error": "expected_revision_must_be_int"},
                              HTTPStatus.BAD_REQUEST)
        reason = str(data.get("reason") or "")[:500]
        with STORE_LOCK:
            try:
                result = self.store.commit(
                    name, block_texts=texts, block_digests=digests,
                    expected_revision=expected, reason=reason)
            except StaleRevision as exc:
                # 过期修订：稳定拒绝，目录/引用数/对象均不改变。
                current = self.store.get_package(name)
                return self._json({
                    "error": "stale_revision",
                    "detail": str(exc),
                    "current_revision": current["revision"] if current else 0,
                }, HTTPStatus.CONFLICT)
            except MissingBlocks as exc:
                return self._json({"error": "missing_blocks", "detail": str(exc)},
                                  HTTPStatus.CONFLICT)
        return self._json({
            "status": "committed",
            "package": result.package,
            "revision": result.revision,
            "added": result.added,
            "removed": result.removed,
            "retained": result.retained,
        }, HTTPStatus.CREATED)

    @staticmethod
    def _package_name_ok(name: str) -> None:
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise ValueError(name)

    def _serve_static(self, name: str, content_type: str) -> None:
        # 禁止路径穿越；优先发布构建产物，缺失时回退到前端源码（开发态）。
        candidates = [STATIC_DIR, STATIC_FALLBACK_DIR]
        body = None
        for base in candidates:
            safe = os.path.normpath(os.path.join(base, name))
            if not (safe == os.path.join(base, name) and
                    os.path.commonpath([safe, base]) == base):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                with open(safe, "rb") as fh:
                    body = fh.read()
                break
            except FileNotFoundError:
                continue
        if body is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(root: str, host: str, port: int) -> ThreadingHTTPServer:
    store = PackageStore(root)
    # 启动即收敛上次 GC 中断，保证存活包不指向缺失内容。
    recovered = store.recover()
    if recovered["removed"] or recovered["cancelled"]:
        sys.stderr.write("[startup] recovery: %s\n" % json.dumps(recovered))
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store  # type: ignore[attr-defined]
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校准帧内容寻址重建包服务")
    parser.add_argument("--host", default=os.environ.get("BIND_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("BIND_PORT", "8080")))
    parser.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    args = parser.parse_args(argv)

    server = build_server(args.data, args.host, args.port)
    sys.stderr.write("[startup] listening on %s:%s data=%s\n"
                     % (args.host, args.port, args.data))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
