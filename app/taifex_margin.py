"""期交所「股價指數類保證金一覽表」：每口原始／維持／結算保證金。

來源：期交所 OpenAPI（只給目前生效中的最新一日，沒有歷史），所以每次抓到就存進 DB，
數字跟上一次不同就記錄一筆變動並發 Telegram。
尚未生效的調整（期交所先公告、之後才生效）這個 API 看不到，生效當天才會反映。

期貨商可以收得比期交所高：若券商實收較高，用 MARGIN_PER_LOT 手動指定每口原始保證金（元）。
"""
import json
import os
import re
import threading
import time
import urllib.request

from .db import DB_
from .notify import notify
from .state import STATE, now

URL = os.getenv("MARGIN_TABLE_URL", "https://openapi.taifex.com.tw/v1/IndexFuturesAndOptionsMargining")
REFRESH_SEC = int(os.getenv("MARGIN_TABLE_REFRESH_MIN", "120")) * 60
KV_KEY = "taifex_margin"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# 商品代碼前三碼 → 期交所表上的商品名稱關鍵字（臺／台兩種寫法都認）
PRODUCTS = {
    "TXF": ("大台", ["臺股期貨", "台股期貨"]),
    "MXF": ("小台", ["小型臺指", "小型台指"]),
    "TMF": ("微台", ["微型臺指", "微型台指", "微型臺股", "微型台股"]),
}
EXCLUDE = ("選擇權", "週", "美元", "電子", "金融", "櫃買", "非金電")


def _num(v):
    if v is None:
        return None
    s = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse(rows):
    """期交所 JSON → {"date": str, "products": {"TMF": {...}}}；認不出來的商品略過。"""
    out, date = {}, None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        name = str(_pick(r, "Contract", "商品別", "商品") or "")
        date = date or _pick(r, "Date", "日期")
        if not name or any(x in name for x in EXCLUDE):
            continue
        for code, (label, kws) in PRODUCTS.items():
            if code in out or not any(k in name for k in kws):
                continue
            init = _num(_pick(r, "InitialMargin", "原始保證金"))
            if not init:
                continue
            out[code] = {
                "label": label, "name": name, "initial": init,
                "maintenance": _num(_pick(r, "MaintenanceMargin", "維持保證金")),
                "clearing": _num(_pick(r, "ClearingMargin", "結算保證金")),
            }
    return {"date": str(date) if date else None, "products": out}


class MarginTable:
    def __init__(self):
        self.lock = threading.RLock()
        self.current = None        # {"date","products","fetched"}
        self.history = []          # 變動紀錄，新的在前
        self.error = None
        self._last_try = 0.0
        self._started = False

    # ------------------------------------------------------------ 狀態
    def restore(self):
        kv = DB_.get_kv(KV_KEY) or {}
        with self.lock:
            self.current = kv.get("current")
            self.history = kv.get("history", [])

    def _persist(self):
        with self.lock:
            DB_.set_kv(KV_KEY, {"current": self.current, "history": self.history[:60]})

    def start(self):
        if self._started:
            return
        self._started = True
        try:
            self.restore()
        except Exception as e:
            STATE.log("WARN", f"讀取保證金表紀錄失敗：{e!r}")
        threading.Thread(target=self._loop, daemon=True, name="taifex-margin").start()

    def _loop(self):
        time.sleep(5)
        while True:
            self.refresh()
            time.sleep(REFRESH_SEC)

    # ------------------------------------------------------------ 抓取
    def refresh(self):
        self._last_try = time.time()
        try:
            req = urllib.request.Request(URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = json.loads(r.read().decode("utf-8-sig"))
            data = parse(rows)
            if not data["products"]:
                raise ValueError("回傳資料裡找不到台指期貨商品（欄位或名稱可能改了）")
        except Exception as e:
            first = self.error is None
            self.error = f"{e!r}"[:200]
            if first:
                STATE.log("WARN", f"期交所保證金表抓取失敗（每 {REFRESH_SEC // 60} 分鐘重試）：{self.error}")
            return False
        if self.error:
            STATE.log("INFO", "期交所保證金表恢復抓取")
        self.error = None
        data["fetched"] = now().strftime("%Y-%m-%d %H:%M")
        with self.lock:
            old = self.current
            changes = self._diff(old, data)
            self.current = data
            if changes:
                self.history.insert(0, {"t": data["fetched"], "date": data["date"], "changes": changes})
        if old is None:
            STATE.log("INFO", "期交所保證金表：" + "，".join(
                f"{p['label']} 原始 {p['initial']:,.0f}" for p in data["products"].values()))
        for c in changes:
            msg = (f"期交所保證金調整：{c['label']} 原始 {c['old_initial']:,.0f} → {c['new_initial']:,.0f}"
                   f"（維持 {c['old_maint'] or 0:,.0f} → {c['new_maint'] or 0:,.0f}），資料日 {data['date']}")
            STATE.log("WARN", msg)
            notify("📋 " + msg + self._capacity_note(c["code"]))
        self._persist()
        return True

    @staticmethod
    def _diff(old, new):
        if not old:
            return []
        out = []
        for code, p in new["products"].items():
            o = (old.get("products") or {}).get(code)
            if not o:
                continue
            if o.get("initial") != p["initial"] or o.get("maintenance") != p["maintenance"]:
                out.append({"code": code, "label": p["label"],
                            "old_initial": o.get("initial") or 0, "new_initial": p["initial"],
                            "old_maint": o.get("maintenance"), "new_maint": p["maintenance"]})
        return out

    def _capacity_note(self, code):
        try:
            from .broker import BROKER
            if not (BROKER.contract and str(BROKER.contract.code).startswith(code)):
                return ""
            m = BROKER.margin
            per = self.per_lot(code)
            if not m or not per:
                return ""
            free = m.get("available_margin", 0)
            return f"\n目前可用 {free:,.0f}，以新標準可再開 {int(free // per)} 口"
        except Exception:
            return ""

    # ------------------------------------------------------------ 查詢
    def product(self, code):
        with self.lock:
            if not self.current or not code:
                return None
            return (self.current.get("products") or {}).get(str(code)[:3])

    def per_lot(self, code):
        """每口原始保證金：MARGIN_PER_LOT 手動值優先，其次期交所表；都沒有回 None。"""
        manual = float(os.getenv("MARGIN_PER_LOT", "0") or 0)
        if manual > 0:
            return manual
        p = self.product(code)
        return p["initial"] if p else None

    def snapshot(self):
        with self.lock:
            return {"current": self.current, "history": self.history[:10], "error": self.error,
                    "manual_per_lot": float(os.getenv("MARGIN_PER_LOT", "0") or 0) or None}


MARGIN_TABLE = MarginTable()
