"""PowerLanguage 語意層。

一支策略 = Strategy 子類別，實作 on_bar(self)。每根 N 分 K 收盤時被呼叫一次，
在裡面用 self.buy_stop(price) 等方法「掛下一根 K 的單」，引擎在下一根 K 期間逐 tick 檢查觸價。

語意對照：
  marketposition            self.mp            （+1 / -1 / 0，每支 1 個單位）
  entryprice(0)             self.entryprice
  maxpositionprofit/bpv     self.maxprofit_pts （每口點數）
  EntriesToday(D)           self.entries_today
  buy next bar market       self.buy_market()
  buy next bar X stop       self.buy_stop(X)
  sellshort next bar X stop self.sellshort_stop(X)
  sell next bar X stop      self.sell_stop(X)   （出場）
  sell next bar X limit     self.sell_limit(X)
  buytocover ... stop/limit self.buytocover_stop/limit(X)
  sell next bar at market   self.sell_market()
  setexitonclose            self.setexitonclose()
  C, C[1], H, L, O, T, D    self.C(0), self.C(1), self.H(), self.L(), self.O(), self.T(), self.D()
  Average(C,n)              self.average(self.C, n)   （series 傳函數）
  AvgRange(n)               self.avgrange(n)
  Highest(H,n)/Lowest(L,n)  self.highest(self.H, n) / self.lowest(self.L, n)
  SwingHigh(1,H,strength,len) self.swinghigh(strength, len) / swinglow
  CloseD(n) / CloseS(n)     self.closeD(n) / self.closeS(n)
  HighW(n) / LowW(n)        self.highW(n) / self.lowW(n)
  CheckDay                  self.checkday   （今天是結算日）
  DayOfWeek(D)              self.D().weekday()+1 % 7 → 用 self.dayofweek()
一根 K 內每支策略最多成交一次（MultiCharts 預設，不開 bar magnifier）。
"""
from . import tf

TICKSIZE = 1.0


class Order:
    __slots__ = ("kind", "action", "price", "label")

    def __init__(self, kind, action, price=None, label=""):
        self.kind = kind        # market / stop / limit
        self.action = action    # buy / sellshort / sell / buytocover
        self.price = price
        self.label = label


class Strategy:
    name = "base"
    minutes = 60
    lots = 1
    inputs = {}

    def __init__(self, cfg):
        self.minutes = int(cfg.get("minutes", self.minutes))
        self.lots = int(cfg.get("lots", self.lots))
        self.enabled = bool(cfg.get("enabled", True))
        self.p = dict(self.inputs)
        self.p.update(cfg.get("inputs", {}))
        # 帳本
        self.mp = 0
        self.mp_prev = 0          # 上一根 K 收盤時的 MP（EL 的 MP[1]）
        self.entryprice = None
        self.maxprofit_pts = 0.0
        self.entries_today = 0
        self.day = None
        self.realized_today = 0.0
        self.trades_today = 0
        self.last_signal = None
        # 掛單
        self.orders = []          # 目前 K 棒有效的掛單
        self.exit_on_close = False
        self.filled_this_bar = False
        # 資料
        self.bars = []
        self.sessions = []
        self.days = []
        self.weeks = []
        self.trading_days = set()
        self._i = 0               # 目前 K 棒索引（on_bar 時 = len(bars)-1）
        self.last_bar_ts = None
        self.log = None           # 由 portfolio 注入

    # ------------------------------------------------------------ 資料存取
    def _s(self, key, n):
        i = self._i - n
        return self.bars[i][key] if i >= 0 else self.bars[0][key]

    def C(self, n=0): return self._s("close", n)
    def H(self, n=0): return self._s("high", n)
    def L(self, n=0): return self._s("low", n)
    def O(self, n=0): return self._s("open", n)
    def T(self): return self.bars[self._i]["time"]
    def D(self): return self.bars[self._i]["date"]
    def dayofweek(self):
        return (self.D().weekday() + 1) % 7   # EL: 0=Sun … 6=Sat

    def average(self, series, n):
        n = max(1, int(n))
        return sum(series(k) for k in range(n)) / n

    def avgrange(self, n):
        n = max(1, int(n))
        return sum(self.H(k) - self.L(k) for k in range(n)) / n

    def highest(self, series, n):
        n = int(n)
        return max(series(k) for k in range(max(1, n)))

    def lowest(self, series, n):
        n = int(n)
        return min(series(k) for k in range(max(1, n)))

    def swinghigh(self, strength, length):
        """SwingHigh(1, H, strength, length)：最近一個左右各 strength 根都較低的高點；找不到回 -1。"""
        for k in range(strength, min(length, self._i + 1) - strength):
            h = self.H(k)
            if all(self.H(k + j) < h for j in range(1, strength + 1)) and \
               all(self.H(k - j) <= h for j in range(1, strength + 1)):
                return h
        return -1

    def swinglow(self, strength, length):
        for k in range(strength, min(length, self._i + 1) - strength):
            l = self.L(k)
            if all(self.L(k + j) > l for j in range(1, strength + 1)) and \
               all(self.L(k - j) >= l for j in range(1, strength + 1)):
                return l
        return -1

    def _day_idx(self):
        d = self.D()
        for i in range(len(self.days) - 1, -1, -1):
            if self.days[i]["date"] <= d:
                return i
        return 0

    def closeD(self, n):
        i = self._day_idx() - n
        if n == 0:
            return self.C()
        return self.days[i]["close"] if i >= 0 else self.days[0]["close"]

    def _ses_idx(self):
        key = self.bars[self._i]["session"]
        for i in range(len(self.sessions) - 1, -1, -1):
            if self.sessions[i]["session"] <= key or self.sessions[i]["session"] == key:
                return i
        return 0

    def closeS(self, n):
        if n == 0:
            return self.C()
        i = self._ses_idx() - n
        return self.sessions[i]["close"] if i >= 0 else self.sessions[0]["close"]

    def _week_idx(self):
        w = self.bars[self._i]["week"]
        for i in range(len(self.weeks) - 1, -1, -1):
            if self.weeks[i]["week"] <= w:
                return i
        return 0

    def _this_week(self, key, fn):
        w = self.bars[self._i]["week"]
        v = self.bars[self._i][key]
        k = self._i - 1
        while k >= 0 and self.bars[k]["week"] == w:
            v = fn(v, self.bars[k][key]); k -= 1
        return v

    def highW(self, n):
        if n == 0:
            return self._this_week("high", max)
        i = self._week_idx() - n
        return self.weeks[i]["high"] if i >= 0 else self.weeks[0]["high"]

    def lowW(self, n):
        if n == 0:
            return self._this_week("low", min)
        i = self._week_idx() - n
        return self.weeks[i]["low"] if i >= 0 else self.weeks[0]["low"]

    @property
    def checkday(self):
        """原碼：結算日 08:45 設 True，到隔天 08:45 才重置 → 結算日的日盤與其後的夜盤都是 True。"""
        b = self.bars[self._i]
        # 該 K 棒所屬「日盤日」：日盤 = 自己的日期；夜盤 = 時段開始那天（前面那個日盤）
        sd = b["date"] if b["is_day"] else tf.parse_ts(b["ts"]).date() if b["time"] >= 1500 else \
            (tf.parse_ts(b["ts"]) - __import__("datetime").timedelta(days=1)).date()
        return tf.settlement_day(sd, self.trading_days)

    def weekend_exit_due(self, tnw):
        """週六（曆法日）K 棒時間 >= TNw 且仍有部位 → 該出場了。目的：不留單過週末。"""
        return self.dayofweek() == 6 and self.T() >= int(tnw) and self.mp != 0

    def exit_market(self, label):
        if self.mp > 0:
            self.sell_market(label)
        elif self.mp < 0:
            self.buytocover_market(label)

    # ------------------------------------------------------------ 掛單 API
    def _add(self, kind, action, price=None, label=""):
        if price is not None and price <= 0:
            return   # SwingHigh 回 -1 之類的無效價位
        self.orders.append(Order(kind, action, price, label))

    def buy_market(self, label="buy"): self._add("market", "buy", None, label)
    def buy_stop(self, price, label="buy stop"): self._add("stop", "buy", price, label)
    def sellshort_market(self, label="sellshort"): self._add("market", "sellshort", None, label)
    def sellshort_stop(self, price, label="sellshort stop"): self._add("stop", "sellshort", price, label)
    def sell_market(self, label="sell"): self._add("market", "sell", None, label)
    def sell_stop(self, price, label="sell stop"): self._add("stop", "sell", price, label)
    def sell_limit(self, price, label="sell limit"): self._add("limit", "sell", price, label)
    def buytocover_market(self, label="buytocover"): self._add("market", "buytocover", None, label)
    def buytocover_stop(self, price, label="buytocover stop"): self._add("stop", "buytocover", price, label)
    def buytocover_limit(self, price, label="buytocover limit"): self._add("limit", "buytocover", price, label)
    def setexitonclose(self): self.exit_on_close = True

    # ------------------------------------------------------------ 引擎呼叫
    def on_bar(self):
        raise NotImplementedError

    def run_bar_close(self, bars, sessions, days, weeks, trading_days):
        """一根 N 分 K 收盤：更新資料、清掉上一根的掛單、跑策略。"""
        self.bars, self.sessions, self.days, self.weeks = bars, sessions, days, weeks
        self.trading_days = trading_days
        self._i = len(bars) - 1
        self.last_bar_ts = bars[-1]["ts"]
        d = self.D()
        if self.day != d:
            self.day = d
            self.entries_today = 0
            self.realized_today = 0.0
            self.trades_today = 0
        # 上一根 K 的 maxprofit 用 K 棒高低更新（tick 也會更新，這裡補漏）
        if self.mp > 0 and self.entryprice is not None:
            self.maxprofit_pts = max(self.maxprofit_pts, self.H() - self.entryprice)
        elif self.mp < 0 and self.entryprice is not None:
            self.maxprofit_pts = max(self.maxprofit_pts, self.entryprice - self.L())
        self.orders = []
        self.exit_on_close = False
        self.filled_this_bar = False
        if self.enabled:
            try:
                self.on_bar()
            except Exception as e:
                if self.log:
                    self.log("ERROR", f"[{self.name}] on_bar 錯誤：{e!r}")
        self.mp_prev = self.mp

    def on_tick(self, price, is_first_tick_of_bar):
        """回 (fill_action, fill_price, label) 或 None。市價單在下一根 K 的第一筆 tick 成交。"""
        if self.mp > 0 and self.entryprice is not None:
            self.maxprofit_pts = max(self.maxprofit_pts, price - self.entryprice)
        elif self.mp < 0 and self.entryprice is not None:
            self.maxprofit_pts = max(self.maxprofit_pts, self.entryprice - price)
        if self.filled_this_bar or not self.orders:
            return None
        for o in self.orders:
            if not self._valid(o):
                continue
            hit = False
            if o.kind == "market":
                hit = True
            elif o.kind == "stop":
                hit = price >= o.price if o.action in ("buy", "buytocover") else price <= o.price
            elif o.kind == "limit":
                hit = price <= o.price if o.action in ("buy", "buytocover") else price >= o.price
            if hit:
                # 市價：當下價；stop/limit：以掛單價成交，若是新 K 第一筆已跳空穿過則以該價成交
                fill = price if (o.kind == "market" or is_first_tick_of_bar) else o.price
                self._apply(o.action, fill)
                self.filled_this_bar = True
                self.orders = []
                return (o.action, fill, o.label)
        return None

    def _valid(self, o):
        if o.action == "buy":
            return self.mp <= 0
        if o.action == "sellshort":
            return self.mp >= 0
        if o.action == "sell":
            return self.mp > 0
        if o.action == "buytocover":
            return self.mp < 0
        return False

    def _apply(self, action, price):
        if action in ("sell", "buytocover"):
            pnl = (price - self.entryprice) * self.mp if self.entryprice is not None else 0
            self.realized_today += pnl
            self.trades_today += 1
            self.mp = 0
            self.entryprice = None
            self.maxprofit_pts = 0.0
            self.last_signal = f"{action} @ {price:.0f} ({pnl:+.0f})"
            return
        # 進場（若持反向部位，MC 會先平再開：這裡直接反手並記損益）
        if self.mp != 0 and self.entryprice is not None:
            pnl = (price - self.entryprice) * self.mp
            self.realized_today += pnl
            self.trades_today += 1
        self.mp = 1 if action == "buy" else -1
        self.entryprice = price
        self.maxprofit_pts = 0.0
        self.entries_today += 1
        self.last_signal = f"{action} @ {price:.0f}"

    def exit_at_close(self, price):
        """setexitonclose：交易日最後一根 K 收盤時被引擎呼叫。"""
        if self.exit_on_close and self.mp != 0:
            action = "sell" if self.mp > 0 else "buytocover"
            self._apply(action, price)
            self.orders = []
            return (action, price, "exit on close")
        return None

    # ------------------------------------------------------------ 序列化
    def to_state(self):
        return {"mp": self.mp, "entryprice": self.entryprice, "maxprofit_pts": self.maxprofit_pts,
                "entries_today": self.entries_today, "day": self.day.isoformat() if self.day else None,
                "realized_today": self.realized_today, "trades_today": self.trades_today,
                "last_signal": self.last_signal}

    def from_state(self, s):
        from datetime import date as _d
        self.mp = int(s.get("mp", 0))
        self.entryprice = s.get("entryprice")
        self.maxprofit_pts = float(s.get("maxprofit_pts", 0))
        self.entries_today = int(s.get("entries_today", 0))
        self.day = _d.fromisoformat(s["day"]) if s.get("day") else None
        self.realized_today = float(s.get("realized_today", 0))
        self.trades_today = int(s.get("trades_today", 0))
        self.last_signal = s.get("last_signal")

    def snapshot(self):
        return {"name": self.name, "minutes": self.minutes, "lots": self.lots, "enabled": self.enabled,
                "mp": self.mp, "entryprice": self.entryprice, "maxprofit_pts": round(self.maxprofit_pts, 1),
                "entries_today": self.entries_today, "realized_today": round(self.realized_today, 1),
                "trades_today": self.trades_today, "last_bar": self.last_bar_ts, "last_signal": self.last_signal,
                "exit_on_close": self.exit_on_close, "bars": len(self.bars),
                "orders": [{"kind": o.kind, "action": o.action, "price": o.price, "label": o.label}
                           for o in self.orders if self._valid(o)],
                "inputs": self.p}
