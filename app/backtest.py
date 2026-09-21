"""回測：用跟即時完全相同的策略語意重播歷史 1 分 K，記錄每筆交易並算績效。

關鍵：進出場判定、觸價成交、K 線組法、交易日規則全部走 el.py / tf.py，
和即時跑的是同一份程式碼，所以回測結果與實盤行為一致（差別只有成本與滑價假設）。

成本模型：
  每口成本 = 手續費(點) + 滑價(點)，進場與出場各算一次。
  微台 1 點 = 10 元，小台 50，大台 200。點數績效乘上 bpv 就是金額。
"""
import math
import threading
import time
from datetime import timedelta

from . import tf
from .db import DB_
from .state import STATE
from .strategies import REGISTRY

BPV = {"TXF": 200, "MXF": 50, "TMF": 10}


class Trade:
    __slots__ = ("strategy", "side", "entry_time", "entry_price", "entry_label",
                 "exit_time", "exit_price", "exit_label", "pts", "net_pts", "bars_held", "mae", "mfe")

    def __init__(self, strategy, side, t, price, label):
        self.strategy = strategy
        self.side = side              # 1 多 / -1 空
        self.entry_time = t
        self.entry_price = price
        self.entry_label = label
        self.exit_time = self.exit_price = self.exit_label = None
        self.pts = self.net_pts = 0.0
        self.bars_held = 0
        self.mae = 0.0                # 最大不利幅度（點）
        self.mfe = 0.0                # 最大有利幅度（點）

    def close(self, t, price, label, cost):
        self.exit_time, self.exit_price, self.exit_label = t, price, label
        self.pts = (price - self.entry_price) * self.side
        self.net_pts = self.pts - cost
        return self

    def as_dict(self, bpv):
        return {"strategy": self.strategy, "side": "多" if self.side > 0 else "空",
                "entry_time": self.entry_time, "entry_price": round(self.entry_price, 1), "entry_label": self.entry_label,
                "exit_time": self.exit_time, "exit_price": round(self.exit_price, 1) if self.exit_price else None,
                "exit_label": self.exit_label, "pts": round(self.pts, 1), "net_pts": round(self.net_pts, 1),
                "money": round(self.net_pts * bpv), "mae": round(self.mae, 1), "mfe": round(self.mfe, 1),
                "minutes": self.bars_held}


def _stats(trades, bpv, equity_curve, mdd_override=None):
    """從交易清單算績效。equity_curve 為逐筆累積淨點數。"""
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.net_pts > 0]
    losses = [t for t in trades if t.net_pts <= 0]
    gp = sum(t.net_pts for t in wins)
    gl = -sum(t.net_pts for t in losses)
    net = gp - gl
    # 最大回撤（以淨點數權益曲線）
    peak, mdd, dd_start, mdd_range = -1e18, 0.0, None, (None, None)
    for i, (t, eq) in enumerate(equity_curve):
        if eq > peak:
            peak, dd_start = eq, t
        dd = peak - eq
        if dd > mdd:
            mdd, mdd_range = dd, (dd_start, t)
    # 連續盈虧
    cw = cl = mw = ml = 0
    for t in trades:
        if t.net_pts > 0:
            cw += 1; cl = 0; mw = max(mw, cw)
        else:
            cl += 1; cw = 0; ml = max(ml, cl)
    longs = [t for t in trades if t.side > 0]
    shorts = [t for t in trades if t.side < 0]
    if mdd_override is not None:
        mdd, mdd_range = mdd_override[0], (mdd_override[1], mdd_override[2])
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1),
        "net_pts": round(net, 1), "net_money": round(net * bpv),
        "gross_profit": round(gp, 1), "gross_loss": round(gl, 1),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "avg_pts": round(net / n, 1),
        "avg_win": round(gp / len(wins), 1) if wins else 0,
        "avg_loss": round(-gl / len(losses), 1) if losses else 0,
        "payoff": round((gp / len(wins)) / (gl / len(losses)), 2) if wins and losses and gl > 0 else 0,
        "expectancy": round(net / n, 1),
        "best": round(max(t.net_pts for t in trades), 1),
        "worst": round(min(t.net_pts for t in trades), 1),
        "max_dd_pts": round(mdd, 1), "max_dd_money": round(mdd * bpv),
        "dd_basis": "floating" if mdd_override is not None else "closed",
        "max_dd_from": mdd_range[0], "max_dd_to": mdd_range[1],
        "recovery_factor": round(net / mdd, 2) if mdd > 0 else 0,
        "max_consec_win": mw, "max_consec_loss": ml,
        "avg_minutes": round(sum(t.bars_held for t in trades) / n),
        "long_trades": len(longs), "long_pts": round(sum(t.net_pts for t in longs), 1),
        "short_trades": len(shorts), "short_pts": round(sum(t.net_pts for t in shorts), 1),
        "avg_mae": round(sum(t.mae for t in trades) / n, 1),
        "avg_mfe": round(sum(t.mfe for t in trades) / n, 1),
    }


def _monthly(trades):
    out = {}
    for t in trades:
        if not t.exit_time:
            continue
        k = t.exit_time[:7]
        out.setdefault(k, {"month": k, "pts": 0.0, "trades": 0, "wins": 0})
        o = out[k]
        o["pts"] += t.net_pts; o["trades"] += 1; o["wins"] += 1 if t.net_pts > 0 else 0
    for o in out.values():
        o["pts"] = round(o["pts"], 1)
        o["win_rate"] = round(o["wins"] / o["trades"] * 100, 1) if o["trades"] else 0
    return sorted(out.values(), key=lambda x: x["month"])


def _sample(curve, n):
    if not curve:
        return []
    if len(curve) <= n:
        return [{"t": t, "eq": round(v, 1)} for t, v in curve]
    step = len(curve) / n
    out = []
    for i in range(n):
        t, v = curve[int(i * step)]
        out.append({"t": t, "eq": round(v, 1)})
    out.append({"t": curve[-1][0], "eq": round(curve[-1][1], 1)})
    return out


class Backtester:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.progress = ""
        self.result = None
        self.error = None

    def status(self):
        return {"running": self.running, "progress": self.progress,
                "has_result": self.result is not None, "error": self.error}

    def start(self, params, portfolio):
        if self.running:
            return False
        threading.Thread(target=self._run, args=(params, portfolio), daemon=True).start()
        return True

    # ------------------------------------------------------------
    def _run(self, params, portfolio):
        self.running, self.error, self.progress = True, None, "載入資料…"
        t0 = time.time()
        try:
            self.result = self._backtest(params, portfolio)
            self.progress = f"完成（{time.time() - t0:.1f} 秒）"
        except Exception as e:
            self.error = repr(e)
            self.progress = "失敗"
            STATE.log("ERROR", f"回測失敗：{e!r}")
        finally:
            self.running = False

    def _backtest(self, params, portfolio):
        start = (params.get("start") or "").strip()
        end = (params.get("end") or "").strip()
        cost = float(params.get("cost_pts", 2))          # 單邊成本（手續費+滑價，點）
        contract = (params.get("contract") or "TMF").upper()
        bpv = BPV.get(contract, 10)
        names = params.get("strategies") or None          # None = 全部啟用中的
        warmup_days = int(params.get("warmup_days", 15))  # 暖機交易日：只跑指標不計交易
        overrides = params.get("inputs") or {}            # {策略名: {參數: 值}}

        m1_all = DB_.load_bars(400000)
        if end:
            m1_all = [b for b in m1_all if b["ts"] <= end + " 23:59"]
        # 暖機：起始日往前多取 warmup_days 個交易日的資料，只餵指標、不計交易
        stats_from = None
        if start and warmup_days > 0:
            trade_dates = sorted({b["ts"][:10] for b in m1_all})
            before = [d for d in trade_dates if d < start]
            warm_start = before[-warmup_days] if len(before) >= warmup_days else (before[0] if before else start)
            m1 = [b for b in m1_all if b["ts"] >= warm_start]
            stats_from = start
            warm_used = len([d for d in before if d >= warm_start])
        elif start:
            m1 = [b for b in m1_all if b["ts"] >= start]
            warm_used = 0
        else:
            m1 = m1_all
            warm_used = 0
            # 沒指定起始日：用資料最前面 warmup_days 個交易日當暖機
            if warmup_days > 0:
                ds = sorted({b["ts"][:10] for b in m1})
                if len(ds) > warmup_days:
                    stats_from = ds[warmup_days]
                    warm_used = warmup_days
        if len(m1) < 100:
            raise ValueError(f"資料不足（{len(m1)} 根 1 分 K），請確認日期區間或等待資料累積")

        # 建立策略實例（獨立於即時跑的那組）
        cfg = portfolio.effective_config()
        strats = []
        for name, cls in REGISTRY.items():
            c = dict(cfg.get(name, {}))
            if names is not None and name not in names:
                continue
            if names is None and not c.get("enabled", name != "demo_ma"):
                continue
            inp = dict(c.get("inputs", {}))
            inp.update(overrides.get(name, {}))
            c["inputs"] = inp
            c["enabled"] = True
            s = cls(c)
            s.log = lambda *a: None
            strats.append(s)
        if not strats:
            raise ValueError("沒有選到任何策略")

        by_tf = {}
        for s in strats:
            by_tf.setdefault(s.minutes, []).append(s)

        self.progress = f"重播 {len(m1)} 根 1 分 K…"
        sessions = tf.build_sessions(m1)
        days = tf.build_days(sessions, m1)
        weeks = tf.build_weeks(days)
        trading_days = {x["date"] for x in sessions if x["is_day"]}
        tfbars = {m: tf.build_bars(m1, m) for m in by_tf}
        idx = {m: 0 for m in by_tf}
        first_tick = {}
        open_trade = {s.name: None for s in strats}
        trades = []
        realized = {"v": 0.0}
        eq_track = {"peak": 0.0, "mdd": 0.0, "from": None, "to": None, "peak_t": None}
        float_curve = []      # 每 N 根 1 分 K 取樣一次的浮動權益（含未實現）

        def counted(ts):
            return stats_from is None or ts[:10] >= stats_from

        def on_fill(s, action, price, label, ts):
            ot = open_trade[s.name]
            if action in ("sell", "buytocover"):
                if ot:
                    t = ot.close(ts, price, label, cost * 2)
                    if counted(t.entry_time):
                        trades.append(t); realized["v"] += t.net_pts
                    open_trade[s.name] = None
            else:
                if ot:   # 反手：先平再開
                    t = ot.close(ts, price, "反手", cost * 2)
                    if counted(t.entry_time):
                        trades.append(t); realized["v"] += t.net_pts
                open_trade[s.name] = Trade(s.name, 1 if action == "buy" else -1, ts, price, label)

        for i, b in enumerate(m1):
            if i % 20000 == 0:
                self.progress = f"重播 {i}/{len(m1)}…"
            ts = b["ts"]
            bar_dt = tf.parse_ts(ts)
            m1_end = bar_dt + timedelta(minutes=1)
            seq = (b["open"], b["high"], b["low"], b["close"]) if b["close"] >= b["open"] \
                else (b["open"], b["low"], b["high"], b["close"])
            for m, ss in by_tf.items():
                for s in ss:
                    ot = open_trade[s.name]
                    if ot:
                        ot.bars_held += 1
                        adverse = (ot.entry_price - b["low"]) if ot.side > 0 else (b["high"] - ot.entry_price)
                        favor = (b["high"] - ot.entry_price) if ot.side > 0 else (ot.entry_price - b["low"])
                        ot.mae = max(ot.mae, adverse); ot.mfe = max(ot.mfe, favor)
                    for px in seq:
                        r = s.on_tick(px, first_tick.get((m, s.name), False), bar_dt)
                        first_tick[(m, s.name)] = False
                        if r:
                            on_fill(s, r[0], r[1], r[2], ts)
                            break
            for m, ss in by_tf.items():
                bars = tfbars[m]
                while idx[m] < len(bars) and tf.parse_ts(bars[idx[m]]["ts"]) <= m1_end:
                    done = bars[:idx[m] + 1]
                    for s in ss:
                        s.run_bar_close(done, sessions, days, weeks, trading_days)
                        first_tick[(m, s.name)] = True
                    idx[m] += 1
            ses = tf.session_of(tf.parse_ts(ts))
            if ses and ses[3] and m1_end >= ses[2]:
                for s in strats:
                    r = s.exit_at_close(b["close"])
                    if r:
                        on_fill(s, r[0], r[1], r[2], ts)
            # 浮動權益（已實現 + 目前所有未平倉的未實現）→ 真正的組合回撤
            if (i % 5 == 0 or i == len(m1) - 1) and counted(ts):
                unreal = 0.0
                for ot in open_trade.values():
                    if ot and counted(ot.entry_time):
                        unreal += (b["close"] - ot.entry_price) * ot.side - cost * 2
                eqv = realized["v"] + unreal
                float_curve.append((ts, eqv))
                if eq_track["peak_t"] is None:
                    eq_track["peak_t"] = ts
                if eqv > eq_track["peak"]:
                    eq_track["peak"], eq_track["peak_t"] = eqv, ts
                dd = eq_track["peak"] - eqv
                if dd > eq_track["mdd"]:
                    eq_track["mdd"], eq_track["from"], eq_track["to"] = dd, eq_track["peak_t"], ts

        self.progress = "計算績效…"
        # 未平倉的：用最後價格結算，標記為持倉中
        last_price, last_ts = m1[-1]["close"], m1[-1]["ts"]
        open_list = []
        for s in strats:
            ot = open_trade[s.name]
            if ot:
                d = Trade(ot.strategy, ot.side, ot.entry_time, ot.entry_price, ot.entry_label)
                d.bars_held, d.mae, d.mfe = ot.bars_held, ot.mae, ot.mfe
                d.close(last_ts, last_price, "持倉中（以最後價估算）", cost * 2)
                open_list.append(d.as_dict(bpv))

        trades.sort(key=lambda t: (t.exit_time or "", t.entry_time or ""))
        eq, curve = 0.0, []
        for t in trades:
            eq += t.net_pts
            curve.append((t.exit_time, eq))

        per = {}
        for s in strats:
            st = [t for t in trades if t.strategy == s.name]
            e, c2 = 0.0, []
            for t in st:
                e += t.net_pts; c2.append((t.exit_time, e))
            per[s.name] = {"name": s.name, "minutes": s.minutes, "lots": s.lots,
                           "inputs": s.p, **_stats(st, bpv, c2), "monthly": _monthly(st)}

        return {
            "params": {"start": (stats_from + " 00:00") if stats_from else m1[0]["ts"],
                       "end": m1[-1]["ts"], "cost_pts": cost,
                       "warmup_days": warm_used, "warmup_from": m1[0]["ts"][:10],
                       "contract": contract, "bpv": bpv, "bars": len(m1),
                       "days": len([d for d in trading_days if stats_from is None or d.isoformat() >= stats_from]),
                       "strategies": [s.name for s in strats]},
            "combined": {**_stats(trades, bpv, curve,
                                  mdd_override=(eq_track["mdd"], eq_track["from"], eq_track["to"])),
                         "monthly": _monthly(trades)},
            "per_strategy": per,
            "equity": _sample(float_curve, 400),
            "trades": [t.as_dict(bpv) for t in trades],
            "open_positions": open_list,
        }


BACKTEST = Backtester()
