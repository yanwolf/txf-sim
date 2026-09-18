"""從 1 分 K 組出 MultiCharts 語意的 N 分 K、交易時段、交易日、週。

台指期時段：日盤 08:45–13:45、夜盤 15:00–翌日 05:00。
N 分 K 以時段開盤為基準切，K 棒標籤用「結束時間」（MultiCharts 慣例）。
K 棒日期由 NIGHT_SESSION_DAY 決定：
  calendar      K 棒收盤時間的曆法日期，過 24:00 換天（Tzu-jen 的 MC 設定，預設）
  next          夜盤整段歸曆法上的隔天
  next_trading  夜盤整段歸下一個交易日（期交所定義）
"""
import os
from datetime import date, datetime, timedelta

NIGHT_SESSION_DAY = os.getenv("NIGHT_SESSION_DAY", "calendar")

DAY_START, DAY_END = 8 * 60 + 45, 13 * 60 + 45
NIGHT_START, NIGHT_END = 15 * 60, 5 * 60


def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%d %H:%M")


def session_of(dt):
    """回 (session_key, session_start_dt, session_end_dt, is_day)；不在時段內回 None。"""
    m = dt.hour * 60 + dt.minute
    d = dt.date()
    if DAY_START <= m < DAY_END or (m == DAY_END and dt.second == 0):
        s = datetime.combine(d, datetime.min.time()) + timedelta(minutes=DAY_START)
        return (d.isoformat() + "D", s, s + timedelta(minutes=DAY_END - DAY_START), True)
    if m >= NIGHT_START:
        s = datetime.combine(d, datetime.min.time()) + timedelta(minutes=NIGHT_START)
        return (d.isoformat() + "N", s, s + timedelta(minutes=24 * 60 - NIGHT_START + NIGHT_END), False)
    if m < NIGHT_END or (m == NIGHT_END and dt.second == 0):
        s = datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(minutes=NIGHT_START)
        return ((d - timedelta(days=1)).isoformat() + "N", s, s + timedelta(minutes=24 * 60 - NIGHT_START + NIGHT_END), False)
    return None


def trading_day(dt, is_day, session_start, bar_end=None):
    """K 棒日期。calendar 模式用 K 棒結束時間的曆法日期。"""
    if NIGHT_SESSION_DAY == "calendar":
        return (bar_end or dt).date()
    if is_day:
        return dt.date()
    nd = session_start.date() + timedelta(days=1)
    if NIGHT_SESSION_DAY == "next_trading":
        while nd.weekday() >= 5:
            nd += timedelta(days=1)
    return nd


def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_bars(m1_bars, minutes):
    """把 1 分 K（由舊到新，ts='YYYY-MM-DD HH:MM' 起始分鐘）組成 N 分 K。

    回傳 list of dict：ts(結束時間), open, high, low, close, volume,
    date(交易日 date), time(HHMM int, 結束時間), session, is_day, week,
    is_session_last(該時段最後一根), is_day_last(交易日最後一根＝日盤 13:45)
    """
    out = []
    cur = None
    for b in m1_bars:
        dt = parse_ts(b["ts"])
        ses = session_of(dt)
        if ses is None:
            continue
        key, s, e, is_day = ses
        idx = int((dt - s).total_seconds() // 60) // minutes
        bar_start = s + timedelta(minutes=idx * minutes)
        bar_end = min(bar_start + timedelta(minutes=minutes), e)
        if cur is None or cur["_key"] != key or cur["_idx"] != idx:
            if cur is not None:
                out.append(cur)
            td = trading_day(dt, is_day, s, bar_end)
            cur = {"_key": key, "_idx": idx, "ts": bar_end.strftime("%Y-%m-%d %H:%M"),
                   "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                   "volume": b["volume"], "date": td, "time": bar_end.hour * 100 + bar_end.minute,
                   "session": key, "is_day": is_day, "week": week_key(td),
                   "is_session_last": bar_end == e, "is_day_last": is_day and bar_end == e}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
    if cur is not None:
        out.append(cur)
    return out


def build_sessions(m1_bars):
    """每個交易時段一筆：high/low/close、交易日、is_day。"""
    return build_bars(m1_bars, 24 * 60)


def build_days(session_bars, m1_bars=None):
    """交易日彙總。calendar 模式用 1 分 K 依「結束時間的曆法日」聚合；其他模式用時段。"""
    out = []
    if NIGHT_SESSION_DAY == "calendar" and m1_bars is not None:
        for b in m1_bars:
            d = (parse_ts(b["ts"]) + timedelta(minutes=1)).date()
            if out and out[-1]["date"] == d:
                x = out[-1]
                x["high"] = max(x["high"], b["high"]); x["low"] = min(x["low"], b["low"]); x["close"] = b["close"]
            else:
                out.append({"date": d, "open": b["open"], "high": b["high"], "low": b["low"],
                            "close": b["close"], "week": week_key(d)})
        return out
    for s in session_bars:
        if out and out[-1]["date"] == s["date"]:
            d = out[-1]
            d["high"] = max(d["high"], s["high"]); d["low"] = min(d["low"], s["low"]); d["close"] = s["close"]
        else:
            out.append({"date": s["date"], "open": s["open"], "high": s["high"], "low": s["low"],
                        "close": s["close"], "week": s["week"]})
    return out


def build_weeks(day_bars):
    out = []
    for d in day_bars:
        if out and out[-1]["week"] == d["week"]:
            w = out[-1]
            w["high"] = max(w["high"], d["high"]); w["low"] = min(w["low"], d["low"]); w["close"] = d["close"]
        else:
            out.append({"week": d["week"], "open": d["open"], "high": d["high"], "low": d["low"], "close": d["close"]})
    return out


def third_wednesday(y, m):
    d = date(y, m, 1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d + timedelta(days=14)


def settlement_day(d, known_trading_days):
    """d 是否為本月結算日：第三個週三；若那天不是交易日（無日盤 K），順延到之後第一個交易日。"""
    tw = third_wednesday(d.year, d.month)
    if d < tw:
        return False
    if d == tw:
        return True
    # d 在第三週三之後：只有在 tw..d-1 之間都沒有交易日時才算順延結算
    x = tw
    while x < d:
        if x in known_trading_days:
            return False
        x += timedelta(days=1)
    return True
