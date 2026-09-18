"""stdlib HTTP 伺服器：儀表板 + JSON API。"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .broker import BROKER
from .state import STATE

HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 關掉預設 access log
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
        elif u.path == "/health":
            self._send(200 if STATE.login_ok else 503, {"ok": STATE.login_ok})
        elif u.path == "/api/status":
            self._send(200, STATE.snapshot())
        elif u.path == "/api/bars":
            n = int(q.get("n", ["60"])[0])
            self._send(200, STATE.recent_bars(n))
        elif u.path == "/api/signals":
            self._send(200, STATE.recent_signals())
        elif u.path == "/api/events":
            self._send(200, STATE.recent_events())
        elif u.path == "/api/orders":
            self._send(200, STATE.recent_orders())
        elif u.path == "/api/strategies":
            from .portfolio import PORTFOLIO
            self._send(200, PORTFOLIO.snapshot())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/kill":
            BROKER.kill("手動")
        elif u.path == "/api/resume":
            BROKER.resume()
        elif u.path == "/api/flat":
            BROKER.flatten("手動平倉")
        elif u.path == "/api/reconcile":
            BROKER.reconcile(manual=True)
        elif u.path == "/api/adopt":
            BROKER.adopt_broker()
        else:
            self._send(404, {"error": "not found"}); return
        self._send(200, {"ok": True})


def serve():
    port = int(os.getenv("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    STATE.log("INFO", f"HTTP 伺服器啟動 :{port}")
    srv.serve_forever()
