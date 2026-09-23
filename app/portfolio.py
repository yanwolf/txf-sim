"""多策略組合：每支策略各自帳本，淨部位才送給下單層。

流程：
  1 分 K 收盤 → 對每個週期組 N 分 K → 有新收完的 N 分 K 就跑該週期的策略（掛下一根的單）
  每筆 tick → 每支策略檢查掛單觸價 → 有成交就重算淨部位 → BROKER.set_net()
  日盤 13:45 最後一根 1 分 K 收盤 → 有 setexitonclose 的策略出場
啟動時用歷史 1 分 K 重播（MultiCharts 重算圖表的概念），策略狀態是歷史的確定函數。
"""
import json
import os
import threading
from datetime import timedelta

from . import tf
from .broker import BROKER
from .db import DB_
from .notify import notify
from .state import STATE, now
from .strategies import REGISTRY

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "strategy_config.json")


class Portfolio:
    def __init__(self):
        self.lock = threading.RLock()
        self.strategies = []
        self.by_tf = {}            # minutes -> [strategy]
        self.last_done_ts = {}     # minutes -> 最後一根已收 N 分 K 的 ts
        self.first_tick = {}       # minutes -> 下一筆 tick 是新 K 第一筆
        self.ready = False
        self.align_pending = False
        self._last_sync = 0.0
        self.load_config()

    # ------------------------------------------------------------ 設定
    def file_config(self):
        try:
            return json.load(open(CONFIG_PATH, encoding="utf-8"))
        except Exception as e:
            STATE.log("ERROR", f"strategy_config.json 讀取失敗：{e!r}")
            return {}

    def effective_config(self):
        """檔案設定 + DB 覆蓋（儀表板存的）。"""
        cfg = {k: v for k, v in self.file_config().items() if not k.startswith("_")}
        over = DB_.get_kv("strategy_config") or {}
        for name, o in over.items():
            base = dict(cfg.get(name, {}))
            base.update({k: v for k, v in o.items() if k != "inputs"})
            inp = dict(base.get("inputs", {}))
            inp.update(o.get("inputs", {}))
            base["inputs"] = inp
            cfg[name] = base
        return cfg

    def load_config(self):
        cfg = self.effective_config()
        self.strategies = []
        self.all_strategies = []
        for name, cls in REGISTRY.items():
            c = cfg.get(name, {})
            s = cls(c)
            s.log = STATE.log
            self.all_strategies.append(s)
            if c.get("enabled", name != "demo_ma"):
                self.strategies.append(s)
        self.by_tf = {}
        for s in self.strategies:
            self.by_tf.setdefault(s.minutes, []).append(s)
        STATE.log("INFO", "策略：" + "、".join(f"{s.name}({s.minutes}分,{s.lots}口{'，實驗' if s.mode == 'paper' else ''})" for s in self.strategies))

    def config_view(self):
        """給設定頁：每支（含停用的）目前設定 + 參數說明。"""
        cfg = self.effective_config()
        out = []
        for name, cls in REGISTRY.items():
            c = cfg.get(name, {})
            inputs = dict(cls.inputs); inputs.update(c.get("inputs", {}))
            out.append({"name": name, "desc": cls.desc, "doc": cls.doc,
                        "enabled": bool(c.get("enabled", name != "demo_ma")), "mode": c.get("mode", "live"),
                        "minutes": int(c.get("minutes", cls.minutes)), "lots": int(c.get("lots", cls.lots)),
                        "inputs": inputs, "defaults": dict(cls.inputs)})
        return out

    def apply_config(self, new_cfg):
        """儀表板儲存：寫 DB、重載策略、背景重播。"""
        clean = {}
        for name, c in new_cfg.items():
            if name not in REGISTRY:
                continue
            cls = REGISTRY[name]
            inp = {}
            for k, v in (c.get("inputs") or {}).items():
                if k in cls.inputs:
                    try:
                        fv = float(v)
                        inp[k] = int(fv) if fv.is_integer() else fv
                    except Exception:
                        pass
            clean[name] = {"enabled": bool(c.get("enabled", True)), "mode": "paper" if c.get("mode") == "paper" else "live",
                           "minutes": max(1, int(c.get("minutes", cls.minutes))), "lots": max(1, int(c.get("lots", cls.lots))),
                           "inputs": inp}
        DB_.set_kv("strategy_config", clean)
        STATE.log("INFO", "策略設定已更新，重新載入並重播")
        notify("策略設定已從儀表板更新，重播中")
        def _reload():
            with self.lock:
                self.ready = False
                self.load_config()
                self.last_done_ts = {}
                self.first_tick = {}
            with STATE.lock:
                m1 = list(STATE.bars)
            self.rebuild(m1)
        threading.Thread(target=_reload, daemon=True).start()
        return clean

    # ------------------------------------------------------------ 資料
    def _context(self, m1):
        sessions = tf.build_sessions(m1)
        days = tf.build_days(sessions, m1)
        weeks = tf.build_weeks(days)
        trading_days = {s["date"] for s in sessions if s["is_day"]}
        return sessions, days, weeks, trading_days

    # ------------------------------------------------------------ 重播
    def rebuild(self, m1):
        """用歷史 1 分 K 從頭重播所有策略。"""
        with self.lock:
            for s in self.strategies:
                s.__init__({"minutes": s.minutes, "lots": s.lots, "inputs": s.p, "enabled": s.enabled, "mode": s.mode})
                s.log = STATE.log
            if not m1:
                self.ready = True
                return
            sessions, days, weeks, trading_days = self._context(m1)
            tfbars = {m: tf.build_bars(m1, m) for m in self.by_tf}
            # 用 1 分 K 當 pseudo tick：每根 1 分 K 收盤 → 檢查哪些 N 分 K 收完 → 跑策略；再用下一根 1 分 K 的 O/H/L/C 觸價
            idx = {m: 0 for m in self.by_tf}
            for i, b in enumerate(m1):
                m1_end = tf.parse_ts(b["ts"]) + timedelta(minutes=1)
                bar_dt = tf.parse_ts(b["ts"])
                # 先用這根 1 分 K 觸價（它是「下一根」的一部分）
                for m, strats in self.by_tf.items():
                    for s in strats:
                        for px in (b["open"], b["high"], b["low"], b["close"]) if b["close"] >= b["open"] else (b["open"], b["low"], b["high"], b["close"]):
                            r = s.on_tick(px, self.first_tick.get((m, s.name), False), bar_dt)
                            self.first_tick[(m, s.name)] = False
                            if r:
                                break
                # 再看有沒有 N 分 K 在這根 1 分 K 結束時收完
                for m, strats in self.by_tf.items():
                    bars = tfbars[m]
                    while idx[m] < len(bars) and tf.parse_ts(bars[idx[m]]["ts"]) <= m1_end:
                        done = bars[:idx[m] + 1]
                        for s in strats:
                            s.run_bar_close(done, sessions, days, weeks, trading_days)
                            self.first_tick[(m, s.name)] = True
                        self.last_done_ts[m] = bars[idx[m]]["ts"]
                        idx[m] += 1
                # 日盤最後一根 1 分 K：exit on close
                ses = tf.session_of(tf.parse_ts(b["ts"]))
                if ses and ses[3] and m1_end >= ses[2]:
                    for s in self.strategies:
                        s.exit_at_close(b["close"])
            self.ready = True
            self.align_pending = True
            net = self.net()
            STATE.log("INFO", f"策略重播完成：{len(m1)} 根 1 分 K，" +
                      "、".join(f"{s.name} MP={s.mp}" for s in self.strategies) + f"，淨部位 {net}")

    # ------------------------------------------------------------ 即時
    def on_m1_close(self, m1, closed_bar):
        if not self.ready:
            return
        with self.lock:
            m1_end = tf.parse_ts(closed_bar["ts"]) + timedelta(minutes=1)
            ctx = None
            for m, strats in self.by_tf.items():
                bars = tf.build_bars(m1, m)
                done = [x for x in bars if tf.parse_ts(x["ts"]) <= m1_end]
                if not done or done[-1]["ts"] == self.last_done_ts.get(m):
                    continue
                if ctx is None:
                    ctx = self._context(m1)
                for s in strats:
                    s.run_bar_close(done, *ctx)
                    self.first_tick[(m, s.name)] = True
                self.last_done_ts[m] = done[-1]["ts"]
                for s in strats:
                    if s.orders:
                        STATE.log("STRAT", f"[{s.name}] {done[-1]['ts']} 收盤 MP={s.mp} 掛單：" +
                                  "；".join(f"{o.action} {o.kind} {o.price if o.price else ''} {o.label}" for o in s.orders if s._valid(o)))
            ses = tf.session_of(tf.parse_ts(closed_bar["ts"]))
            if ses and ses[3] and m1_end >= ses[2]:
                for s in self.strategies:
                    r = s.exit_at_close(closed_bar["close"])
                    if r:
                        self._fill(s, *r)
            self._persist()

    def on_tick(self, price):
        if not self.ready:
            return
        with self.lock:
            if self.align_pending and BROKER.ready:
                self.align_pending = False
                self._send_net(price, "啟動對齊")
            else:
                # 券商部位與策略淨部位不同（例如 kill 解除後、或委託失敗過）：每 60 秒嘗試對齊一次
                import time as _t
                if _t.time() - self._last_sync > 60:
                    self._last_sync = _t.time()
                    with STATE.lock:
                        pos, kill, halted, pending = STATE.position, STATE.kill, STATE.strategy_halted, STATE.pending_order
                    if BROKER.ready and not kill and not halted and not pending and pos != self.net():
                        self._send_net(price, "部位對齊")
            for m, strats in self.by_tf.items():
                for s in strats:
                    key = (m, s.name)
                    r = s.on_tick(price, self.first_tick.get(key, False), now())
                    self.first_tick[key] = False
                    if r:
                        self._fill(s, *r)

    def _fill(self, s, action, price, label):
        side = {"buy": "多", "sellshort": "空", "sell": "平多", "buytocover": "平空"}[action]
        tag = f"[{s.name}{'·實驗' if s.mode == 'paper' else ''}]"
        STATE.add_signal(side, price, f"{tag} {label}")
        STATE.log("SIGNAL", f"{tag} {side} @ {price:.0f} {label} → MP={s.mp}")
        notify(f"{tag} {side} @ {price:.0f} {label}")
        if s.mode != "paper":
            self._send_net(price, f"[{s.name}] {label}")
        self._persist()

    def net(self):
        return sum(s.mp * s.lots for s in self.strategies if s.mode != "paper")

    def _send_net(self, price, reason):
        BROKER.set_net(self.net(), price, reason)

    def _persist(self):
        try:
            DB_.set_kv("strategies", {s.name: s.to_state() for s in self.strategies})
        except Exception:
            pass

    def snapshot(self):
        if not self.lock.acquire(timeout=0.5):
            return {"ready": False, "net": 0, "strategies": [], "busy": True}
        try:
            return {"ready": self.ready, "net": self.net(),
                    "strategies": [s.snapshot() for s in self.strategies]}
        finally:
            self.lock.release()


PORTFOLIO = Portfolio()
