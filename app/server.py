"""stdlib HTTP 伺服器：儀表板 + JSON API。"""
import hmac
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .broker import BROKER
from .state import STATE

HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
TOKENS = set()


def _authed(handler):
    if not PASSWORD:
        return True
    return handler.headers.get("X-Token", "") in TOKENS


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
        elif u.path == "/api/config":
            from .portfolio import PORTFOLIO
            self._send(200, {"protected": bool(PASSWORD), "authed": _authed(self), "strategies": PORTFOLIO.config_view()})
        elif u.path == "/api/backtest/status":
            from .backtest import BACKTEST
            self._send(200, BACKTEST.status())
        elif u.path == "/api/backtest/result":
            from .backtest import BACKTEST
            r = BACKTEST.result
            if r is None:
                self._send(200, {"empty": True}); return
            if q.get("full", ["0"])[0] == "1":
                self._send(200, r); return
            light = {k: v for k, v in r.items() if k not in ("trades", "equity")}
            light["trades"] = r["trades"][-300:]
            light["trade_count"] = len(r["trades"])
            light["equity"] = r["equity"][-400:] if len(r["equity"]) > 400 else r["equity"]
            self._send(200, light)
        elif u.path == "/api/backtest/csv":
            from .backtest import BACKTEST
            r = BACKTEST.result
            if r is None:
                self._send(404, {"error": "尚無回測結果"}); return
            cols = ["strategy", "side", "entry_time", "entry_price", "entry_label",
                    "exit_time", "exit_price", "exit_label", "pts", "net_pts", "money", "mae", "mfe"]
            head = "策略,方向,進場時間,進場價,進場原因,出場時間,出場價,出場原因,點數,淨點數,金額,MAE,MFE\n"
            body = "".join(",".join(str(t.get(c, "")).replace(",", " ") for c in cols) + "\n" for t in r["trades"])
            data = ("\ufeff" + head + body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="backtest_trades.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/auth":
            self._send(200, {"protected": bool(PASSWORD), "authed": _authed(self)})
        else:
            self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/login":
            body = self._body()
            if PASSWORD and hmac.compare_digest(str(body.get("password", "")), PASSWORD):
                t = secrets.token_hex(16); TOKENS.add(t)
                self._send(200, {"ok": True, "token": t}); return
            if not PASSWORD:
                self._send(200, {"ok": True, "token": ""}); return
            self._send(401, {"ok": False, "error": "密碼錯誤"}); return
        if not _authed(self):
            self._send(401, {"ok": False, "error": "需要密碼"}); return
        if u.path == "/api/config":
            from .portfolio import PORTFOLIO
            body = self._body()
            PORTFOLIO.apply_config(body.get("strategies", {}))
            self._send(200, {"ok": True}); return
        if u.path == "/api/backtest/run":
            from .backtest import BACKTEST
            from .portfolio import PORTFOLIO
            if not BACKTEST.start(self._body(), PORTFOLIO):
                self._send(409, {"ok": False, "error": "回測進行中"}); return
            self._send(200, {"ok": True}); return
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
