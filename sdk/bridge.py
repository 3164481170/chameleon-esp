"""可选的本地 HTTP 数据桥。

用途：
    * 在浏览器里看实时目标列表 / 调试信息（http://127.0.0.1:PORT/state）
    * 供后续做 Web 版雷达、日志面板、录制回放
默认只监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional


class Bridge:
    def __init__(self, state_provider: Callable[[], Dict[str, Any]], port: int = 17655,
                 log=None, host: str = "127.0.0.1"):
        self.provider = state_provider
        self.host = host
        self.port = port
        self.log = log
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        provider = self.provider
        log = self.log

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默
                if log:
                    log.debug("bridge: " + (args[0] % args[1:]))

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/state"):
                    data = provider()
                    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                    return self._send(200, body)
                if self.path.startswith("/health"):
                    return self._send(200, b'{"ok":true}')
                html = ("<html><head><meta charset='utf-8'><title>Chameleon ESP Bridge</title></head>"
                        "<body style='font-family:monospace;background:#111;color:#0f0'>"
                        "<h3>Chameleon ESP Bridge</h3>"
                        "<p>实时数据：<a style='color:#0ff' href='/state'>/state</a></p>"
                        "<pre id='s'></pre><script>"
                        "async function t(){const r=await fetch('/state');"
                        "document.getElementById('s').textContent=JSON.stringify(await r.json(),null,1);}"
                        "setInterval(t,200);t();</script></body></html>")
                self._send(200, html.encode("utf-8"), "text/html")

        try:
            self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        except Exception as exc:
            if self.log:
                self.log.warn("数据桥启动失败：%s" % exc)
            return False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="esp-bridge", daemon=True)
        self.thread.start()
        if self.log:
            self.log.info("数据桥已启动：http://%s:%d/state" % (self.host, self.port))
        return True

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        self.thread = None
