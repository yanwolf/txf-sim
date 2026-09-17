"""Shioaji 連線引擎（模擬模式、只收行情、只出訊號）。"""
import os
import threading
import time
from datetime import datetime, timedelta

import shioaji as sj

from . import strategy
from .state import STATE, TZ, in_session, now

API_KEY = os.getenv("SHIOAJI_API_KEY", "")
SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "")
CONTRACT_CODE = os.getenv("CONTRACT_CODE", "TXF").upper()
SIMULATION = os.getenv("SIMULATION", "true").lower() != "false"
WARMUP_DAYS = int(os.getenv("WARMUP_DAYS", "3"))
STALE_SECONDS = int(os.getenv("STALE_SECONDS", "180"))
MIN_RELOGIN_GAP = 300  # 秒。避免重連風暴撞到每日登入次數上限


class Engine:
    def __init__(self):
        self.api = None
        self.contract = None
        self._cur_bar = None       # 正在累積的 1 分 K
        self._last_relogin = 0.0
        self._login_ts = 0.0
        self._legacy = False
        self._day = None

    # ------------------------------------------------------------ 連線
    def start(self):
        STATE.simulation = SIMULATION
        if not API_KEY or not SECRET_KEY:
            STATE.log("ERROR", "缺少 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY 環境變數")
            return
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def _run(self):
        self._connect()
        while True:
            time.sleep(30)
            self._watchdog()

    def _connect(self):
        if time.time() - self._last_relogin < MIN_RELOGIN_GAP and STATE.login_count > 0:
            STATE.log("WARN", "距上次登入不到 5 分鐘，先不重連")
            return
        self._last_relogin = time.time()
        self._login_ts = time.time()
        try:
            if self.api is not None:
                try:
                    self.api.logout()
                except Exception:
                    pass
            self.api = sj.Shioaji(simulation=SIMULATION)
            accounts = self.api.login(api_key=API_KEY, secret_key=SECRET_KEY)
            self._legacy = hasattr(self.api, "quote")  # shioaji < 1.7 的舊介面
            with STATE.lock:
                STATE.login_ok = True
                STATE.login_count += 1
                STATE.last_login_at = now().isoformat(timespec="seconds")
                STATE.last_login_error = None
                STATE.accounts = [
                    {"type": type(a).__name__, "broker_id": a.broker_id,
                     "account_id": a.account_id, "signed": getattr(a, "signed", None)}
                    for a in (accounts or [])
                ]
            STATE.log("INFO", f"登入成功（{'模擬' if SIMULATION else '正式'}），帳號 {len(accounts or [])} 個")
        except Exception as e:
            with STATE.lock:
                STATE.login_ok = False
                STATE.last_login_error = repr(e)
            STATE.log("ERROR", f"登入失敗：{e!r}")
            return

        self._pick_contract()
        if self.contract is None:
            return
        self._warmup()
        self._subscribe()
        self._refresh_usage()

    def _pick_contract(self):
        try:
            if not self._legacy:
                self.api.fetch_contracts(contract_download=True, contracts_timeout=30000)
            group = getattr(self.api.Contracts.Futures, CONTRACT_CODE)
            c = group.get(f"{CONTRACT_CODE}R1") if hasattr(group, "get") else getattr(group, f"{CONTRACT_CODE}R1", None)
            if c is None:
                # 沒有 R1 連續合約就自己挑最近的未到期月合約
                digits = lambda v: "".join(ch for ch in str(v) if ch.isdigit())
                today = digits(now().date())
                cands = [x for x in group
                         if len(x.code) == len(CONTRACT_CODE) + 2 and digits(x.delivery_date) >= today]
                c = sorted(cands, key=lambda x: digits(x.delivery_date))[0]
            self.contract = c
            g = lambda k, d=None: getattr(c, k, d)
            num = lambda v: float(v) if v not in (None, "", 0) else None
            with STATE.lock:
                STATE.contract = {
                    "code": g("code"), "symbol": g("symbol", g("code")), "name": g("name", ""),
                    "delivery_date": str(g("delivery_date", "")),
                    "last_trading_date": str(g("last_trading_date", "")),
                    "reference": num(g("reference")),
                    "limit_up": num(g("limit_up")),
                    "limit_down": num(g("limit_down")),
                }
                if STATE.contract["reference"]:
                    STATE.prev_close = STATE.contract["reference"]
            STATE.log("INFO", f"合約：{g('code')} {g('name', '')} 結算日 {g('delivery_date', '')}")
        except Exception as e:
            self.contract = None
            STATE.log("ERROR", f"找不到合約 {CONTRACT_CODE}R1：{e!r}（合約檔可能尚未下載完成）")

    def _warmup(self):
        try:
            end = now().date()
            start = end - timedelta(days=WARMUP_DAYS)
            k = self.api.kbars(self.contract, start=start.isoformat(), end=end.isoformat())
            bars = []
            for ts, o, h, l, c, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume):
                # Shioaji 的 ts 已是台北時間（以 UTC 形式存），且標的是該分鐘的「結束」時間
                # 轉成跟即時 K 一致的「起始分鐘」標籤
                dt = datetime.utcfromtimestamp(ts / 1e9) - timedelta(minutes=1)
                bars.append({"ts": dt.strftime("%Y-%m-%d %H:%M"), "open": float(o), "high": float(h),
                             "low": float(l), "close": float(c), "volume": int(v), "src": "hist"})
            with STATE.lock:
                STATE.bars.clear()
                STATE.bars.extend(bars)
                STATE.warmup_bars = len(bars)
                STATE.warmup_error = None
                if bars and not STATE.prev_close:
                    STATE.prev_close = bars[-1]["close"]
            STATE.log("INFO", f"暖機回補 1 分 K {len(bars)} 根（{start} ~ {end}）")
        except Exception as e:
            with STATE.lock:
                STATE.warmup_error = repr(e)
            STATE.log("WARN", f"暖機回補失敗：{e!r}")

    def _subscribe(self):
        try:
            if self._legacy:
                self.api.quote.set_on_tick_fop_v1_callback(lambda exchange, tick: self._on_tick(tick))
                self.api.quote.set_event_callback(self._on_event)
                self.api.quote.subscribe(self.contract, quote_type=sj.constant.QuoteType.Tick,
                                         version=sj.constant.QuoteVersion.v1)
            else:
                self.api.set_on_tick_fop_v1_callback(self._on_tick)
                self.api.set_event_callback(self._on_event)
                self.api.set_session_down_callback(self._on_session_down)
                self.api.subscribe(self.contract, quote_type=sj.constant.QuoteType.Tick,
                                   version=sj.constant.QuoteVersion.v1)
            with STATE.lock:
                STATE.subscribed = True
            STATE.log("INFO", f"已訂閱 {self.contract.code} Tick")
        except Exception as e:
            with STATE.lock:
                STATE.subscribed = False
            STATE.log("ERROR", f"訂閱失敗：{e!r}")

    def _refresh_usage(self):
        try:
            u = self.api.usage()
            with STATE.lock:
                STATE.usage = {
                    "connections": u.connections,
                    "bytes_mb": round(u.bytes / 1e6, 2),
                    "limit_mb": round(u.limit_bytes / 1e6, 1),
                    "remaining_mb": round(u.remaining_bytes / 1e6, 1),
                }
        except Exception as e:
            STATE.log("WARN", f"查詢 usage 失敗：{e!r}")

    # ------------------------------------------------------------ 回呼
    def _on_event(self, resp_code, event_code, info, event):
        STATE.log("EVENT", f"[{resp_code}/{event_code}] {event} {info}")

    def _on_session_down(self):
        STATE.log("WARN", "Shioaji 回報 session down，看門狗會在下一輪重連")
        with STATE.lock:
            STATE.subscribed = False

    def _on_tick(self, tick):
        try:
            if getattr(tick, "simtrade", 0):
                return  # 試撮，不算
            price = float(tick.close)
            vol = int(tick.volume)
            dt = tick.datetime
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            with STATE.lock:
                STATE.tick_count += 1
                STATE.last_tick_at = dt.strftime("%H:%M:%S")
                STATE.last_tick_ts = time.time()
                STATE.last_price = price
                d = dt.date()
                if self._day != d:
                    self._day = d
                    STATE.day_open, STATE.day_high, STATE.day_low, STATE.day_volume = price, price, price, 0
                STATE.day_high = max(STATE.day_high, price)
                STATE.day_low = min(STATE.day_low, price)
                STATE.day_volume += vol
            self._aggregate(dt, price, vol)
        except Exception as e:
            STATE.log("ERROR", f"tick 處理錯誤：{e!r}")

    def _aggregate(self, dt, price, vol):
        minute = dt.strftime("%Y-%m-%d %H:%M")
        closed = None
        if self._cur_bar is None or self._cur_bar["ts"] != minute:
            closed = self._cur_bar
            self._cur_bar = {"ts": minute, "open": price, "high": price, "low": price,
                             "close": price, "volume": vol, "src": "live"}
        else:
            b = self._cur_bar
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            b["volume"] += vol

        if closed is not None:
            with STATE.lock:
                # 若暖機資料已有同一分鐘，用即時的覆蓋
                if STATE.bars and STATE.bars[-1]["ts"] == closed["ts"]:
                    STATE.bars.pop()
                STATE.bars.append(closed)
                bars = list(STATE.bars)
            try:
                strategy.on_bar_close(bars)
            except Exception as e:
                STATE.log("ERROR", f"策略錯誤：{e!r}")

    # ------------------------------------------------------------ 看門狗
    def _watchdog(self):
        if not STATE.login_ok:
            STATE.log("WARN", "未登入，嘗試重連")
            self._connect()
            return
        if int(time.time()) % 300 < 30:
            self._refresh_usage()
        ref = max(STATE.last_tick_ts, self._login_ts)
        if in_session(now()) and STATE.subscribed and time.time() - ref > STALE_SECONDS:
            STATE.log("WARN", f"盤中 {STALE_SECONDS} 秒沒有 tick，重新連線")
            with STATE.lock:
                STATE.subscribed = False
            self._connect()


ENGINE = Engine()
