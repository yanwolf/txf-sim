"""Shioaji 連線引擎（模擬模式、只收行情、只出訊號）。"""
import os
import threading
import time
from datetime import datetime, timedelta

import base64
import shioaji as sj

from . import strategy
from .broker import BROKER, MODE
from .db import DB_
from .notify import notify
from .state import STATE, TZ, in_session, now

API_KEY = os.getenv("SHIOAJI_API_KEY", "")
SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY", "")
CONTRACT_CODE = os.getenv("CONTRACT_CODE", "TXF").upper()
ORDER_CONTRACT_CODE = os.getenv("ORDER_CONTRACT_CODE", CONTRACT_CODE).upper()   # 訊號看 TXF、下單下 TMF 之類
SIMULATION = os.getenv("SIMULATION", "true").lower() != "false"
WARMUP_DAYS = int(os.getenv("WARMUP_DAYS", "3"))
STALE_SECONDS = int(os.getenv("STALE_SECONDS", "180"))
MIN_RELOGIN_GAP = 300  # 秒。避免重連風暴撞到每日登入次數上限
LIVE_CONFIRM = os.getenv("LIVE_CONFIRM", "") == "YES"
CA_PFX_BASE64 = os.getenv("CA_PFX_BASE64", "")
CA_PASSWORD = os.getenv("CA_PASSWORD", "")
PERSON_ID = os.getenv("PERSON_ID", "")


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
        if MODE == "live" and (SIMULATION or not LIVE_CONFIRM):
            STATE.log("ERROR", "MODE=live 需要 SIMULATION=false 且 LIVE_CONFIRM=YES，已拒絕啟動")
            return
        if MODE == "sim" and not SIMULATION:
            STATE.log("ERROR", "MODE=sim 但 SIMULATION=false，已拒絕啟動（避免誤下實單）")
            return
        # DB：先連、還原狀態與 K 線
        DB_.connect()
        STATE.db = DB_.status()
        if DB_.ok:
            STATE.log("INFO", f"DB 就緒（{DB_.kind}）")
            DB_.trim()
            saved = DB_.load_bars(600)
            with STATE.lock:
                STATE.bars.extend(saved)
            for sg in reversed(DB_.load_signals(200)):
                STATE.signals.appendleft(sg)
            if saved:
                STATE.log("INFO", f"從 DB 還原 K 線 {len(saved)} 根")
        else:
            STATE.log("WARN", f"DB 無法使用：{DB_.error}（狀態只存記憶體）")
        BROKER.restore()
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def _run(self):
        try:
            self._connect()
        except BaseException as e:   # 含 Rust panic
            STATE.log("ERROR", f"連線流程崩潰：{e!r}")
        while True:
            time.sleep(30)
            try:
                self._watchdog()
            except BaseException as e:
                STATE.log("ERROR", f"看門狗例外：{e!r}")

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
            notify(f"⚠️ 登入失敗：{e!r}", key="loginfail", cooldown=600)
            return

        self._activate_ca()
        self._pick_contract()
        if self.contract is None:
            return
        self._warmup()
        self._subscribe()
        self._refresh_usage()
        BROKER.attach(self.api, self._order_contract(), self._legacy)
        notify("已登入並訂閱行情" + ("，重連" if STATE.login_count > 1 else ""), key="login", cooldown=60)

    def _activate_ca(self):
        if not CA_PFX_BASE64:
            return
        try:
            path = "/tmp/ca.pfx"
            with open(path, "wb") as f:
                f.write(base64.b64decode(CA_PFX_BASE64))
            ok = self.api.activate_ca(ca_path=path, ca_passwd=CA_PASSWORD, person_id=PERSON_ID or None)
            STATE.log("INFO", f"憑證啟用：{ok}")
        except Exception as e:
            STATE.log("ERROR", f"憑證啟用失敗：{e!r}")

    def _order_contract(self):
        """R1 是連續合約代號，下單要用實際月合約（如 TXFJ6）。下單商品可與行情商品不同。"""
        try:
            code = ORDER_CONTRACT_CODE
            group = getattr(self.api.Contracts.Futures, code)
            digits = lambda v: "".join(ch for ch in str(v) if ch.isdigit())
            # 先試該商品的 R1 指向的月合約
            r1 = group.get(f"{code}R1") if hasattr(group, "get") else getattr(group, f"{code}R1", None)
            target = getattr(r1, "target_code", None) if r1 is not None else None
            if target:
                c = group.get(target) if hasattr(group, "get") else getattr(group, target, None)
                if c is not None:
                    return c
            today = digits(now().date())
            cands = [x for x in group if len(x.code) == len(code) + 2 and digits(x.delivery_date) >= today]
            return sorted(cands, key=lambda x: digits(x.delivery_date))[0]
        except Exception as e:
            STATE.log("ERROR", f"找不到下單用月合約 {ORDER_CONTRACT_CODE}：{e!r}")
            return None

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
            BROKER.on_tick(price)
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
            DB_.upsert_bar(closed)
            try:
                strategy.on_bar_close(bars)
            except Exception as e:
                STATE.log("ERROR", f"策略錯誤：{e!r}")

    # ------------------------------------------------------------ 看門狗
    def _watchdog(self):
        STATE.db = DB_.status()
        BROKER.tick_watchdog()
        if not STATE.login_ok:
            STATE.log("WARN", "未登入，嘗試重連")
            self._connect()
            return
        if int(time.time()) % 300 < 30:
            self._refresh_usage()
        ref = max(STATE.last_tick_ts, self._login_ts)
        if in_session(now()) and STATE.subscribed and time.time() - ref > STALE_SECONDS:
            STATE.log("WARN", f"盤中 {STALE_SECONDS} 秒沒有 tick，重新連線")
            notify(f"⚠️ 盤中 {STALE_SECONDS} 秒沒有 tick，重新連線", key="stale", cooldown=600)
            with STATE.lock:
                STATE.subscribed = False
            self._connect()


ENGINE = Engine()
