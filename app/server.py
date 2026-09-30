"""stdlib HTTP 伺服器：儀表板 + JSON API。"""
import hmac
import json
import time
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .broker import BROKER
from .state import STATE

HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_MIN = int(os.getenv("DASHBOARD_SESSION_MIN", "30"))   # 登入幾分鐘後自動變回唯讀
TOKENS = {}                     # token -> 到期時間（epoch 秒）
_TOKENS_LOADED = False


def _load_tokens():
    global _TOKENS_LOADED
    if _TOKENS_LOADED:
        return
    try:
        from .db import DB_
        saved = DB_.get_kv("dashboard_tokens")
        if isinstance(saved, dict):                  # 舊版存的是 list，直接捨棄（需重新登入一次）
            TOKENS.update({k: float(v) for k, v in saved.items()})
        _TOKENS_LOADED = True
    except Exception:
        pass


def _save_tokens():
    try:
        from .db import DB_
        now_ = time.time()
        live = {k: v for k, v in TOKENS.items() if v > now_}
        TOKENS.clear(); TOKENS.update(live)
        DB_.set_kv("dashboard_tokens", live)
    except Exception:
        pass


def _token_left(handler):
    """回傳這個請求的登入剩餘秒數；未登入或過期回 0。"""
    _load_tokens()
    t = handler.headers.get("X-Token", "")
    exp = TOKENS.get(t, 0)
    left = exp - time.time()
    if exp and left <= 0:
        TOKENS.pop(t, None); _save_tokens()
    return max(0, left)


def _authed(handler):
    if not PASSWORD:
        return True
    return _token_left(handler) > 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 關掉預設 access log
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass                                    # 手機切走頁面，正常
        except Exception as e:
            self._report(e)

    def do_POST(self):
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._report(e)

    _err_seen = {}

    def _report(self, e):
        key = f"{self.path.split('?')[0]}:{type(e).__name__}"
        n = Handler._err_seen.get(key, 0) + 1
        Handler._err_seen[key] = n
        if n == 1 or n % 100 == 0:
            STATE.log("ERROR", f"儀表板 API 錯誤 {self.path.split('?')[0]}（第 {n} 次）：{e!r}")
        try:
            self._send(500, {"error": repr(e)})
        except Exception:
            pass

    def _do_GET(self):
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
            from .backtest import BACKTEST, max_bars, _available_mb
            self._send(200, {**BACKTEST.status(), "max_bars": max_bars(), "avail_mb": round(_available_mb())})
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
        elif u.path == "/api/hist/months":
            from .db import DB_
            y = q.get("year", [""])[0][:4]
            ms = DB_.hist_months(y) if y.isdigit() else []
            # 以該年中位數為基準判斷偏少：日盤每日根數、夜盤每日根數
            for m in ms:
                m["day_avg"] = round(m["day"] / m["day_days"]) if m["day_days"] else 0
                m["night_avg"] = round(m["night"] / m["night_days"]) if m["night_days"] else 0
            med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
            md = med([m["day_avg"] for m in ms if m["day_avg"]])
            mn = med([m["night_avg"] for m in ms if m["night_avg"]])
            for m in ms:
                notes = []
                if m["bars"] == 0:
                    m["notes"] = ["整月無資料"]
                    continue
                if m["day_days"] < 12:
                    notes.append(f"交易日只有 {m['day_days']} 天")
                if md and m["day_avg"] and m["day_avg"] < md * 0.75:
                    notes.append("日盤每日根數偏少")
                if mn and m["night_days"] == 0:
                    notes.append("整月沒有夜盤")
                elif mn and m["night_avg"] and m["night_avg"] < mn * 0.75:
                    notes.append("夜盤每日根數偏少")
                elif mn and m["day_days"] and m["night_days"] < m["day_days"] * 0.7:
                    notes.append(f"夜盤只有 {m['night_days']} 天")
                m["notes"] = notes
            self._send(200, {"year": y, "months": ms, "day_median": md, "night_median": mn}); return
        elif u.path == "/api/hist/summary":
            from .db import DB_
            ys = DB_.hist_summary()
            full = [y["bars"] for y in ys if y["bars"] > 0]
            med = sorted(full)[len(full) // 2] if full else 0
            for y in ys:                      # 明顯偏少（不到中位數 75%）就標記，方便找出缺漏的年份
                y["thin"] = bool(med and y["bars"] < med * 0.75)
            self._send(200, {"years": ys, "median": med}); return
        elif u.path == "/api/margin":
            if not _authed(self):
                self._send(200, {"locked": True}); return
            from .broker import BROKER, MARGIN_CHECK
            from .taifex_margin import MARGIN_TABLE
            _S = STATE
            if q.get("refresh", ["0"])[0] == "1":
                BROKER.refresh_margin()
                MARGIN_TABLE.refresh()
            code = _S.order_contract
            per = MARGIN_TABLE.per_lot(code)
            snap = MARGIN_TABLE.snapshot()
            m = BROKER.margin
            self._send(200, {"locked": False, "margin": m, "table": snap,
                             "order_code": code, "per_lot": per,
                             "per_lot_source": "manual" if snap["manual_per_lot"] else "taifex",
                             "lots_affordable": int(m["available_margin"] // per) if (m and per) else None,
                             "check": MARGIN_CHECK,
                             "blocked": _S.margin_blocked}); return
        elif u.path == "/api/holidays":
            from .state import ENV_HOLIDAYS, SAVED_HOLIDAYS, HOLIDAYS
            f = lambda xs: sorted(x.isoformat() for x in xs)
            self._send(200, {"env": f(ENV_HOLIDAYS), "saved": f(SAVED_HOLIDAYS), "effective": f(HOLIDAYS)})
        elif u.path == "/api/auth":
            left = _token_left(self) if PASSWORD else 0
            self._send(200, {"protected": bool(PASSWORD), "authed": _authed(self), "left_sec": int(left)})
        else:
            self._send(404, {"error": "not found"})

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def _do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/login":
            body = self._body()
            if PASSWORD and hmac.compare_digest(str(body.get("password", "")), PASSWORD):
                _load_tokens()
                t = secrets.token_hex(16); TOKENS[t] = time.time() + SESSION_MIN * 60; _save_tokens()
                self._send(200, {"ok": True, "token": t, "left_sec": SESSION_MIN * 60}); return
            if not PASSWORD:
                self._send(200, {"ok": True, "token": ""}); return
            self._send(401, {"ok": False, "error": "密碼錯誤"}); return
        if u.path == "/api/logout":
            _load_tokens()
            TOKENS.pop(self.headers.get("X-Token", ""), None); _save_tokens()
            self._send(200, {"ok": True}); return
        if not _authed(self):
            self._send(401, {"ok": False, "error": "需要密碼"}); return
        if u.path == "/api/config":
            from .portfolio import PORTFOLIO
            body = self._body()
            PORTFOLIO.apply_config(body.get("strategies", {}))
            self._send(200, {"ok": True}); return
        if u.path == "/api/hist/upload":
            from .db import DB_
            from .histimport import parse_chunk
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n > 8 * 1024 * 1024:
                self._send(413, {"ok": False, "error": "單塊超過 8MB"}); return
            raw = self.rfile.read(n)
            mode = "start" if q.get("label", ["end"])[0] == "start" else "end"
            from .histimport import iter_texts
            tot = {"rows": 0, "bad": 0, "off_session": 0, "suspicious": 0, "first": None, "last": None}
            for _, text in iter_texts(raw):
                rows, st = parse_chunk(text, mode)
                DB_.upsert_hist(rows)
                for k in ("rows", "bad", "off_session", "suspicious"):
                    tot[k] += st[k]
                for k, cmp_ in (("first", min), ("last", max)):
                    if st[k]:
                        tot[k] = st[k] if tot[k] is None else cmp_(tot[k], st[k])
            self._send(200, {"ok": True, **tot}); return
        if u.path == "/api/hist/url":
            from .db import DB_
            from .histimport import fetch, iter_texts, parse_chunk
            body = self._body()
            label = "start" if body.get("label") == "start" else "end"
            out = []
            for url in [x.strip() for x in str(body.get("urls", "")).replace(",", "\n").split("\n") if x.strip()]:
                try:
                    data, name = fetch(url)
                    for fn, text in iter_texts(data, name):
                        tot = {"rows": 0, "bad": 0, "off_session": 0, "suspicious": 0, "first": None, "last": None}
                        buf = []
                        size = 0
                        for line in text.splitlines(True):          # 分批解析，避免一次吃掉太多記憶體
                            buf.append(line); size += len(line)
                            if size >= 4 * 1024 * 1024:
                                rows, st = parse_chunk("".join(buf), label); DB_.upsert_hist(rows)
                                for k in ("rows", "bad", "off_session", "suspicious"):
                                    tot[k] += st[k]
                                for k, cmp_ in (("first", min), ("last", max)):
                                    if st[k]:
                                        tot[k] = st[k] if tot[k] is None else cmp_(tot[k], st[k])
                                buf, size = [], 0
                        if buf:
                            rows, st = parse_chunk("".join(buf), label); DB_.upsert_hist(rows)
                            for k in ("rows", "bad", "off_session", "suspicious"):
                                tot[k] += st[k]
                            for k, cmp_ in (("first", min), ("last", max)):
                                if st[k]:
                                    tot[k] = st[k] if tot[k] is None else cmp_(tot[k], st[k])
                        out.append({"name": fn, "ok": True, **tot})
                        STATE.log("INFO", f"歷史資料匯入 {fn}：{tot['rows']:,} 根（{tot['first']} ~ {tot['last']}）")
                except Exception as e:
                    out.append({"name": url[:60], "ok": False, "error": str(e)})
                    STATE.log("WARN", f"歷史資料匯入失敗 {url[:60]}：{e}")
            self._send(200, {"ok": True, "files": out}); return
        if u.path == "/api/hist/clear":
            from .db import DB_
            DB_.hist_clear()
            STATE.log("WARN", "已清除全部 MC 歷史資料")
            self._send(200, {"ok": True}); return
        if u.path == "/api/holidays":
            from .state import parse_date_list, set_saved_holidays, HOLIDAYS
            from .db import DB_
            dates, bad = parse_date_list(self._body().get("text", ""))
            if bad:
                self._send(400, {"ok": False, "error": "無法解析：" + "、".join(bad[:5])}); return
            set_saved_holidays(dates)
            DB_.set_kv("market_holidays", sorted(x.isoformat() for x in dates))
            STATE.log("INFO", f"休市日已更新（儀表板）：共 {len(HOLIDAYS)} 天")
            self._send(200, {"ok": True, "effective": sorted(x.isoformat() for x in HOLIDAYS)}); return
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
