"""關卡守住率研究（給「選擇權賣方選履約價」用的診斷，不交易、不影響任何策略）。

問題：Orbit 的階梯形成後，價格在接下來 H 個交易日守住「階梯 ∓ X%」的機率，
      是否明顯高於「同樣距離、隨便一條線」？

規則（2026-11-02 跑之前寫定，不准看結果後改）：
- 事件：用 v1 已校準的指標層（OrbitCalc，類別預設參數）逐根跑 30 分 K。
  多方階梯「換新」（long_ts 改變）且階梯 < 收盤 → 多方事件；空方鏡像（階梯 > 收盤）。
  距離 d% = |收盤 − 階梯| ÷ 收盤。
- 順勢：多方事件時 收盤 > MA1、MA2 與公道伯（v1 的 bull 條件）；空方鏡像。
- 期間：H = 1、3、5 個交易日（夜盤歸下一個交易日；從事件下一根 30 分 K 起算到第 H 個交易日收盤）
- 緩衝 X% = 0、0.5、1.0（相當於賣在階梯外多遠的履約價）
- 路徑守住：期間內最低價 > 階梯 − X（多方）；收盤守住：期間最後收盤 > 階梯 − X（選擇權到期看結算價，這項較重要）
- 基準：同一期間內「每一根 30 分 K」都當作起點，算同樣的跌幅分布；
  一個距離 d% 的事件，基準守住率＝所有起點中「跌幅 < d% + X%」的比例（距離配對，不是隨便抽）
- 主要指標：收盤守住、H=3、X=0.5%
- 判定：主要指標「實際 − 基準」在 2017～2023 與 2024～ 兩段都 ≥ +5 個百分點（至少一個方向、全部事件）
        → 關卡對選擇權賣方有參考價值，才值得抓選擇權歷史資料；否則不往下做
"""
import bisect

HORIZONS = (1, 3, 5)
BUFFERS = (0.0, 0.5, 1.0)
PRIMARY = (3, 0.5)


def _trading_day_index(bars):
    """每根 K 的交易日序號：日盤＝當天；夜盤歸下一個日盤（週五夜盤歸週一、連假前夜盤歸收假日）。"""
    order = []
    for b in bars:
        if not order or order[-1] != b["session"]:
            order.append(b["session"])
    ses_day = {}
    nxt = None
    for key in reversed(order):
        if key.endswith("D"):
            nxt = key[:10]
            ses_day[key] = nxt
        else:
            ses_day[key] = nxt                 # 最後一段夜盤後面沒有日盤 → None
    days = sorted({d for d in ses_day.values() if d})
    di = {d: i for i, d in enumerate(days)}
    return [di.get(ses_day[b["session"]]) for b in bars], days


class _Sparse:
    """區間最小/最大值 O(1) 查詢。"""

    def __init__(self, arr, fn):
        self.fn = fn
        self.t = [list(arr)]
        k = 1
        while (1 << k) <= len(arr):
            prev = self.t[-1]
            half = 1 << (k - 1)
            self.t.append([fn(prev[i], prev[i + half]) for i in range(len(arr) - (1 << k) + 1)])
            k += 1

    def q(self, lo, hi):                      # 含 lo、hi
        k = (hi - lo + 1).bit_length() - 1
        return self.fn(self.t[k][lo], self.t[k][hi - (1 << k) + 1])


def run(m1, stats_from, progress=lambda s: None):
    from . import tf
    from .orbit import OrbitCalc
    from .strategies import OrbitCost30

    progress("組 30 分 K…")
    bars = tf.build_bars(m1, 30)
    sessions = tf.build_sessions(m1)
    trading_days = {x["date"] for x in sessions if x["is_day"]}
    tdi, _ = _trading_day_index(bars)
    n = len(bars)
    if n < 500:
        raise ValueError("資料太少，無法做關卡研究")

    # 每根 K 的「第 H 個交易日最後一根」索引
    last_of_day = {}
    for i, d in enumerate(tdi):
        if d is not None:
            last_of_day[d] = i
    end_idx = {H: [None] * n for H in HORIZONS}
    for i, d in enumerate(tdi):
        if d is None:
            continue
        for H in HORIZONS:
            j = last_of_day.get(d + H)
            if j is not None and j > i:
                end_idx[H][i] = j

    progress("建立區間查詢…")
    lows = _Sparse([b["low"] for b in bars], min)
    highs = _Sparse([b["high"] for b in bars], max)

    def moves(i, H):
        j = end_idx[H][i]
        if j is None:
            return None
        c = bars[i]["close"]
        lo, hi, ce = lows.q(i + 1, j), highs.q(i + 1, j), bars[j]["close"]
        return ((c - lo) / c * 100, (hi - c) / c * 100, (c - ce) / c * 100, (ce - c) / c * 100)

    def period(ts):
        return "2017～2023" if ts[:4] < "2024" else "2024～"

    # 基準分布：每根 K 當起點（統計區間內）
    progress("計算基準分布…")
    base = {}            # (period, H) -> dict(dn, up, edn, eup) 排序後 list
    for i, b in enumerate(bars):
        if stats_from and b["ts"][:10] < stats_from:
            continue
        for H in HORIZONS:
            m = moves(i, H)
            if m is None:
                continue
            for per in (period(b["ts"]), "全期"):
                d = base.setdefault((per, H), {"dn": [], "up": [], "edn": [], "eup": []})
                d["dn"].append(m[0]); d["up"].append(m[1]); d["edn"].append(m[2]); d["eup"].append(m[3])
    for d in base.values():
        for k in d:
            d[k].sort()

    def frac_below(arr, x):
        return bisect.bisect_left(arr, x) / len(arr) if arr else None

    # 事件
    progress("跑指標層找階梯事件…")
    calc = OrbitCalc(dict(OrbitCost30.inputs))
    last_long = last_short = None
    events = []
    for i, b in enumerate(bars):
        calc.update(b, trading_days)
        if calc.ma2 is None:
            last_long, last_short = calc.long_ts, calc.short_ts
            continue
        c = b["close"]
        if stats_from is None or b["ts"][:10] >= stats_from:
            if calc.long_ts != last_long and calc.long_ladder is not None and calc.long_ladder < c:
                trend = c > calc.ma1 and c > calc.ma2 and (calc.avl is None or c > calc.avl)
                events.append((i, 1, (c - calc.long_ladder) / c * 100, trend))
            if calc.short_ts != last_short and calc.short_ladder is not None and calc.short_ladder > c:
                trend = c < calc.ma1 and c < calc.ma2 and (calc.avl is None or c < calc.avl)
                events.append((i, -1, (calc.short_ladder - c) / c * 100, trend))
        last_long, last_short = calc.long_ts, calc.short_ts
        if i % 20000 == 0:
            progress(f"跑指標層找階梯事件… {i * 100 // n}%")

    progress("統計守住率…")
    acc = {}
    for i, side, dist, trend in events:
        ts = bars[i]["ts"]
        for ctx in ("全部", "順勢") if trend else ("全部",):
            for per in (period(ts), "全期"):
                for H in HORIZONS:
                    m = moves(i, H)
                    if m is None:
                        continue
                    bd = base.get((per, H))
                    if not bd:
                        continue
                    path_mv, end_mv = (m[0], m[2]) if side > 0 else (m[1], m[3])
                    bpath, bend = (bd["dn"], bd["edn"]) if side > 0 else (bd["up"], bd["eup"])
                    for X in BUFFERS:
                        key = ("多" if side > 0 else "空", ctx, per, H, X)
                        a = acc.setdefault(key, [0, 0, 0.0, 0, 0.0, []])
                        a[0] += 1
                        a[1] += 1 if path_mv < dist + X else 0
                        a[2] += frac_below(bpath, dist + X)
                        a[3] += 1 if end_mv < dist + X else 0
                        a[4] += frac_below(bend, dist + X)
                        if X == 0.0:
                            a[5].append(dist)

    rows = []
    order_per = {"2017～2023": 0, "2024～": 1, "全期": 2}
    for (side, ctx, per, H, X), a in acc.items():
        nn = a[0]
        dists = sorted(acc[(side, ctx, per, H, 0.0)][5])
        rows.append({"side": side, "ctx": ctx, "period": per, "H": H, "X": X, "n": nn,
                     "dist_med": round(dists[len(dists) // 2], 2) if dists else None,
                     "path": round(a[1] / nn * 100, 1), "path_base": round(a[2] / nn * 100, 1),
                     "close": round(a[3] / nn * 100, 1), "close_base": round(a[4] / nn * 100, 1)})
    rows.sort(key=lambda r: (r["side"] != "多", r["ctx"] != "全部", order_per[r["period"]], r["H"], r["X"]))

    verdict = []
    for side in ("多", "空"):
        diffs = {}
        for per in ("2017～2023", "2024～"):
            r = next((r for r in rows if r["side"] == side and r["ctx"] == "全部" and r["period"] == per
                      and r["H"] == PRIMARY[0] and r["X"] == PRIMARY[1]), None)
            diffs[per] = None if r is None else round(r["close"] - r["close_base"], 1)
        ok = all(v is not None and v >= 5 for v in diffs.values())
        verdict.append({"side": side, "diff_early": diffs["2017～2023"], "diff_late": diffs["2024～"], "pass": ok})
    return {"kind": "levels", "events": len(events), "bars30": n, "rows": rows, "verdict": verdict,
            "passed": any(v["pass"] for v in verdict), "primary": {"H": PRIMARY[0], "X": PRIMARY[1]}}
