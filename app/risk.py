"""風控提示：目前的權益數撐不撐得住「同方向最大口數 + 再來一次最大回撤」。

算法
- 同方向最大口數：正式上線（mode=live、啟用中）的策略，依 sides 加總多方、空方可能的口數，取大的。
  例：TMFF（多空）+ GuYuan_2024（只做多）+ GuYuan_2024_S（只做空）→ 多 2 口、空 2 口 → 2 口。
- 最大回撤：實際帳戶（券商成交）的已實現最大回撤點數，已包含多策略同時持倉的時段；
  RISK_MDD_PTS 可手動指定（例如想用回測或 MultiCharts 的更長期數字），且取兩者較大者。
- 回撤緩衝倍數 =（權益數 − 同方向口數 × 每口原始保證金）÷（最大回撤點數 × 每點價值）
  ≥ 3 安全、2~3 注意、1~2 偏高、< 1 危險。

環境變數
- RISK_MDD_MULT：目標緩衝倍數（預設 3）
- RISK_MDD_PTS：手動最大回撤點數（預設 0 = 只用實際帳戶）
- RISK_MDD_MIN_PTS：實際紀錄太少時的下限（預設 1500）
- RISK_ALERT：on/off，風險等級變差時發 Telegram（預設 on）
"""
import os
import threading
import time

from .notify import notify
from .state import STATE

MULT = float(os.getenv("RISK_MDD_MULT", "3") or 3)
MANUAL_MDD = float(os.getenv("RISK_MDD_PTS", "0") or 0)
MIN_MDD = float(os.getenv("RISK_MDD_MIN_PTS", "1500") or 1500)
ALERT = os.getenv("RISK_ALERT", "on").lower() != "off"

LEVELS = [  # (下限倍數, 代碼, 中文, 說明)
    (3.0, "ok", "安全", "可承受 3 次以上最大回撤"),
    (2.0, "notice", "注意", "緩衝低於 3 倍，不建議再加口數"),
    (1.0, "high", "偏高", "再來兩次最大回撤就可能追繳，考慮補錢或減少上線策略"),
    (float("-inf"), "danger", "危險", "再來一次最大回撤就可能追繳，建議補錢或把部分策略退回 paper"),
]
RANK = {"ok": 0, "notice": 1, "high": 2, "danger": 3}

_lock = threading.Lock()
_mdd_cache = {"t": 0.0, "pts": None}
_last_level = {"code": None}


def _bpv(code):
    code = str(code or "TMF")
    return 50 if code.startswith("MXF") else 200 if code.startswith("TXF") else 10


def account_mdd():
    """實際帳戶最大回撤（點），10 分鐘快取。"""
    with _lock:
        if _mdd_cache["pts"] is not None and time.time() - _mdd_cache["t"] < 600:
            return _mdd_cache["pts"]
    try:
        from .perf import account_curve, _mdd, LIVE_SINCE
        acc = account_curve(LIVE_SINCE, 10)
        pts = _mdd([c["eq"] for c in acc["curve"]])
    except Exception:
        pts = None
    with _lock:
        _mdd_cache.update(t=time.time(), pts=pts)
    return pts


def exposure():
    """正式上線策略的同方向最大口數。"""
    from .portfolio import PORTFOLIO
    longs, shorts, names = 0, 0, []
    for s in getattr(PORTFOLIO, "strategies", []):
        if s.mode == "paper" or not getattr(s, "enabled", True):
            continue
        side = getattr(s, "sides", "both")
        if side in ("both", "long"):
            longs += s.lots
        if side in ("both", "short"):
            shorts += s.lots
        names.append({"name": s.name, "sides": side, "lots": s.lots})
    return {"long": longs, "short": shorts, "worst": max(longs, shorts), "strategies": names}


def assess(margin, per_lot, maint_per_lot, order_code):
    """回傳風控評估；資料不足時回 None。"""
    if not margin or not per_lot:
        return None
    bpv = _bpv(order_code)
    exp = exposure()
    acct = account_mdd()
    mdd_pts = max(acct or 0, MANUAL_MDD, MIN_MDD)
    src = "手動" if MANUAL_MDD and MANUAL_MDD >= (acct or 0) and MANUAL_MDD >= MIN_MDD else \
          "實際帳戶" if acct and acct >= MIN_MDD else "下限值"
    mdd_ntd = mdd_pts * bpv
    equity = float(margin.get("equity") or 0)
    worst = exp["worst"]
    margin_need = worst * per_lot
    ratio = round((equity - margin_need) / mdd_ntd, 2) if mdd_ntd else None
    code, label, note = next((c, l, n) for lo, c, l, n in LEVELS if ratio is not None and ratio >= lo) \
        if ratio is not None else ("ok", "—", "")
    target = margin_need + MULT * mdd_ntd
    after_one = equity - mdd_ntd
    maint_need = worst * (maint_per_lot or 0)
    return {
        "level": code, "label": label, "note": note,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "mult": MULT, "bpv": bpv,
        "worst_lots": worst, "long_lots": exp["long"], "short_lots": exp["short"], "strategies": exp["strategies"],
        "mdd_pts": round(mdd_pts), "mdd_ntd": round(mdd_ntd), "mdd_src": src, "account_mdd_pts": acct,
        "equity": round(equity), "margin_need": round(margin_need),
        "target_equity": round(target), "shortfall": round(max(0.0, target - equity)),
        "warn_line": round(margin_need + 2 * mdd_ntd),           # 跌破這條就進入「偏高」
        "after_one_mdd": round(after_one), "maint_need": round(maint_need),
        "call_after_one": bool(maint_need and after_one < maint_need),
    }


def check_alert(margin):
    """保證金更新後呼叫：風險等級變差就推 Telegram（恢復安全也通知一次）。"""
    if not ALERT or not margin:
        return
    try:
        from .taifex_margin import MARGIN_TABLE
        code = STATE.order_contract
        p = MARGIN_TABLE.product(code)
        r = assess(margin, MARGIN_TABLE.per_lot(code), p["maintenance"] if p else None, code)
    except Exception:
        return
    if not r or r["ratio"] is None:
        return
    prev = _last_level["code"]
    _last_level["code"] = r["level"]
    if prev is None:
        return
    if RANK[r["level"]] > RANK[prev] and r["level"] in ("high", "danger"):
        icon = "🛑" if r["level"] == "danger" else "⚠️"
        msg = (f"{icon} 風控{r['label']}：回撤緩衝 {r['ratio']:.1f} 倍（目標 {r['mult']:.0f} 倍）\n"
               f"權益 {r['equity']:,} ／ 同方向最多 {r['worst_lots']} 口 ／ 最大回撤 {r['mdd_pts']:,} 點\n"
               f"{r['note']}；補到 {r['mult']:.0f} 倍需約 {r['shortfall']:,} 元")
        STATE.log("WARN", msg.replace("\n", "，"))
        notify(msg, key="risk_" + r["level"], cooldown=6 * 3600)
    elif r["level"] == "ok" and prev in ("high", "danger"):
        notify(f"✅ 風控回到安全：回撤緩衝 {r['ratio']:.1f} 倍", key="risk_ok", cooldown=3600)
    elif r["level"] != prev:
        STATE.log("INFO", f"風控等級 {prev} → {r['level']}（緩衝 {r['ratio']:.1f} 倍）")
