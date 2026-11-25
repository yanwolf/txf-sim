"""實盤績效統計：從資料庫裡的成交（fills）與策略訊號（signals）重算。

兩條線：
1. 帳戶實際損益：只看 live 委託的券商成交，以平均成本法逐筆算已實現點數 → 真正進出帳戶的錢（未扣手續費／稅）。
2. 各策略帳本：只看「正式上線」（訊號標籤沒有 ·實驗）的進出場，以訊號價配對 → 跟 MultiCharts 一樣的帳本口徑。
   兩者差額 ≈ 滑價 + 淨部位互抵（多策略同時進出時，帳戶只下淨部位）。
"""
import os
import re
from collections import OrderedDict

from .db import DB_

LIVE_SINCE = os.getenv("LIVE_SINCE", "2026-09-23")      # 從 MultiCharts 移植、txf-sim 正式下單的第一天
FUT_PREFIX = ("TMF", "MXF", "TXF")                       # 只算期貨成交（零股回報不算）
TAG = re.compile(r"^\[([^\]·]+)(·實驗)?\]\s*(.*)$")


def _rows(sql, params=()):
    try:
        return DB_.run(sql, params, fetch=True) or []
    except Exception:
        return []


def account_curve(start, bpv):
    """live 委託的實際成交 → 每筆平倉的已實現點數。"""
    live_ids = set()
    for oid, bid in _rows("SELECT id, broker_id FROM orders WHERE mode = ?", ("live",)):
        if oid:
            live_ids.add(str(oid))
        if bid:
            live_ids.add(str(bid))
    fills = _rows("SELECT t, order_id, code, action, price, qty FROM fills WHERE t >= ? ORDER BY t, id", (start,))
    pos, avg, cum = 0, None, 0.0
    closes, days = [], OrderedDict()
    n_fills = 0
    for t, oid, code, action, price, qty in fills:
        if not code or not str(code).startswith(FUT_PREFIX):
            continue
        if live_ids and str(oid) not in live_ids:
            continue
        n_fills += 1
        q = int(qty) * (1 if action == "Buy" else -1)
        price = float(price)
        while q:
            if pos == 0 or (pos > 0) == (q > 0):          # 開倉／加碼
                avg = price if pos == 0 else (avg * abs(pos) + price * abs(q)) / (abs(pos) + abs(q))
                pos += q
                q = 0
            else:                                         # 平倉（可能反手）
                n = min(abs(q), abs(pos))
                pts = (price - avg) * n * (1 if pos > 0 else -1)
                cum += pts
                closes.append({"t": t[:16], "pts": round(pts, 1), "eq": round(cum, 1)})
                days[t[:10]] = round(cum, 1)
                pos += n if pos < 0 else -n
                q += n if q < 0 else -n
                if pos == 0:
                    avg = None
    return {"fills": n_fills, "closes": len(closes), "net_pts": round(cum, 1), "net_ntd": round(cum * bpv),
            "open_pos": pos, "open_avg": avg, "curve": closes, "daily": [{"d": d, "eq": v} for d, v in days.items()],
            "win": sum(1 for c in closes if c["pts"] > 0)}


def _unfilled_index(start):
    """live 委託（含重送）與「漲跌停暫不送單」事件，用來找帳本有進場、帳戶卻沒成交的交易。"""
    from datetime import datetime
    P = lambda t: datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
    orders = [(P(t), reason or "", int(filled or 0) if status != "failed" else 0)
              for t, reason, status, filled in _rows(
                  "SELECT t, reason, status, filled_qty FROM orders WHERE mode = ? AND t >= ?", ("live", start))]
    locks = [(P(t), msg or "") for t, msg in _rows(
        "SELECT t, msg FROM events WHERE t >= ? AND (msg LIKE ? OR msg LIKE ?)",
        (start, "%停%暫不送單%", "%停%委託未成交%"))]
    return orders, locks


def _mark_unfilled(trades, name, idx):
    """進場前後 5 秒內，帶這支策略標籤的 live 委託全部 0 成交（含重送），或有漲跌停暫不送單 → 標成未成交。
    多策略同時進場只下淨部位時，委託理由會用「＋」串起來，所以用「包含標籤」判斷。"""
    from datetime import datetime
    orders, locks = idx
    tag = f"[{name}]"
    for x in trades:
        et = datetime.strptime(x["entry_t"][:19], "%Y-%m-%d %H:%M:%S")
        near = lambda t: -2 <= (t - et).total_seconds() <= 5
        if any(near(t) and tag in m for t, m in locks):
            x["unfilled"] = "漲跌停"
            continue
        mine = [f for t, r, f in orders if near(t) and tag in r]
        if mine and sum(mine) == 0:
            x["unfilled"] = "委託未成交"


def strategy_ledgers(start):
    """正式上線訊號 → 各策略逐筆交易（訊號價）。"""
    sigs = _rows("SELECT t, side, price, reason FROM signals WHERE t >= ? ORDER BY t, id", (start,))
    idx = _unfilled_index(start)
    open_ = {}
    trades = {}
    for t, side, price, reason in sigs:
        m = TAG.match(reason or "")
        if not m or m.group(2):                  # 沒標籤（舊示範策略）或 ·實驗＝paper，不算
            continue
        name, label = m.group(1), m.group(3)
        price = float(price)
        cur = open_.get(name)
        if side in ("多", "空"):
            d = 1 if side == "多" else -1
            if cur and cur["d"] != d:            # 反手：先把反向部位以同價平掉
                trades.setdefault(name, []).append(_close(cur, t, price, "反手"))
                cur = None
            if cur is None:
                open_[name] = {"d": d, "t": t, "p": price, "lab": label}
        elif side in ("平多", "平空") and cur:
            trades.setdefault(name, []).append(_close(cur, t, price, label))
            open_.pop(name, None)
    out = {}
    for name in sorted(set(trades) | set(open_)):
        ts = trades.get(name, [])
        _mark_unfilled(ts, name, idx)
        uf = [x for x in ts if x.get("unfilled")]
        cum, curve = 0.0, []
        for x in ts:
            cum += x["pts"]
            curve.append({"t": x["exit_t"][:16], "eq": round(cum, 1)})
        wins = [x["pts"] for x in ts if x["pts"] > 0]
        loss = [x["pts"] for x in ts if x["pts"] <= 0]
        o = open_.get(name)
        out[name] = {"trades": len(ts), "net_pts": round(cum, 1), "win": len(wins),
                     "win_rate": round(len(wins) * 100 / len(ts), 1) if ts else 0,
                     "pf": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) < 0 else None,
                     "max_dd": _mdd([c["eq"] for c in curve]),
                     "first": ts[0]["entry_t"][:10] if ts else (o["t"][:10] if o else None),
                     "open": ({"side": "多" if o["d"] > 0 else "空", "t": o["t"][:16], "price": o["p"]} if o else None),
                     "unfilled_n": len(uf), "unfilled_pts": round(sum(x["pts"] for x in uf), 1),
                     "unfilled": [x for x in uf][::-1],
                     "curve": curve, "last": ts[-8:][::-1]}
    return out


def _close(cur, t, price, label):
    return {"d": cur["d"], "entry_t": cur["t"], "entry": cur["p"], "exit_t": t, "exit": price,
            "pts": round((price - cur["p"]) * cur["d"], 1), "entry_lab": cur["lab"], "exit_lab": label}


def _mdd(eq):
    peak, dd = 0.0, 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return round(dd, 1)


def compute(start=None, bpv=10):
    start = (start or LIVE_SINCE)[:10]
    acc = account_curve(start, bpv)
    acc["max_dd"] = _mdd([c["eq"] for c in acc["curve"]])
    strats = strategy_ledgers(start)
    ledger_total = round(sum(s["net_pts"] for s in strats.values()), 1)
    uf_n = sum(s["unfilled_n"] for s in strats.values())
    uf_pts = round(sum(s["unfilled_pts"] for s in strats.values()), 1)
    return {"start": start, "bpv": bpv, "account": acc, "strategies": strats, "ledger_total": ledger_total,
            "unfilled_n": uf_n, "unfilled_pts": uf_pts,
            "slippage_gap": round(acc["net_pts"] - ledger_total, 1)}
