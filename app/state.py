"""執行期共享狀態：引擎寫入、HTTP 伺服器讀取。"""
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")


def now():
    return datetime.now(TZ)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.started_at = now().isoformat(timespec="seconds")
        self.mode = "signal"
        self.simulation = True

        self.login_ok = False
        self.login_count = 0
        self.last_login_at = None
        self.last_login_error = None
        self.accounts = []

        self.contract = None          # dict(code, name, delivery_date, ...)
        self.subscribed = False

        self.warmup_bars = 0
        self.warmup_error = None

        self.tick_count = 0
        self.last_tick_at = None      # 台北時間 isoformat
        self.last_tick_ts = 0.0       # epoch, 給 watchdog 用
        self.last_price = None
        self.prev_close = None        # 前一日收盤（暖機資料最後一根）
        self.day_open = None
        self.day_high = None
        self.day_low = None
        self.day_volume = 0

        self.usage = None             # api.usage() 結果

        self.bars = deque(maxlen=80000)  # 1 分 K，dict(ts, open, high, low, close, volume)；約 60 個交易日
        self.signals = deque(maxlen=200)
        self.events = deque(maxlen=300)

        self.position = 0             # 內部認定的部位（口數，正多負空）
        self.position_price = None    # 均價
        self.virtual_pnl = 0.0        # 訊號模式：虛擬已實現損益（點）
        self.realized_pnl = 0.0       # 下單模式：今日已實現損益（點/口）
        self.broker_position = None   # 券商回報的部位
        self.reconcile_ok = None
        self.mismatch_min = 0
        self.session_note = None     # 例如「本時段無成交，推定休市」
        self.last_tick_session = None
        self.order_contract = None    # 實際下單用的月合約代碼
        self.pending_order = None
        self.kill = False
        self.kill_reason = None
        self.strategy_halted = None
        self.orders = deque(maxlen=100)
        self.db = None

    # ---- 寫入 ----
    def log(self, level, msg):
        e = {"t": now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "msg": str(msg)}
        with self.lock:
            self.events.appendleft(e)
        print(f"[{level}] {msg}", flush=True)
        try:
            from .db import DB_
            DB_.add_event(e)
        except Exception:
            pass

    def add_signal(self, side, price, reason):
        s = {"t": now().strftime("%Y-%m-%d %H:%M:%S"), "side": side, "price": price, "reason": reason}
        with self.lock:
            self.signals.appendleft(s)
        try:
            from .db import DB_
            DB_.add_signal(s)
        except Exception:
            pass

    # ---- 讀取 ----
    def snapshot(self):
        with self.lock:
            change = None
            if self.last_price is not None and self.prev_close:
                change = round(self.last_price - self.prev_close, 1)
            return {
                "started_at": self.started_at,
                "mode": self.mode,
                "simulation": self.simulation,
                "login_ok": self.login_ok,
                "login_count": self.login_count,
                "last_login_at": self.last_login_at,
                "last_login_error": self.last_login_error,
                "accounts": self.accounts,
                "contract": self.contract,
                "subscribed": self.subscribed,
                "warmup_bars": self.warmup_bars,
                "warmup_error": self.warmup_error,
                "tick_count": self.tick_count,
                "last_tick_at": self.last_tick_at,
                "tick_age_sec": round(time.time() - self.last_tick_ts) if self.last_tick_ts else None,
                "last_price": self.last_price,
                "prev_close": self.prev_close,
                "change": change,
                "day_open": self.day_open,
                "day_high": self.day_high,
                "day_low": self.day_low,
                "day_volume": self.day_volume,
                "usage": self.usage,
                "bar_count": len(self.bars),
                "position": self.position,
                "position_price": self.position_price,
                "virtual_pnl": round(self.virtual_pnl, 1),
                "realized_pnl": round(self.realized_pnl, 1),
                "unrealized_pnl": round((self.last_price - self.position_price) * self.position, 1)
                    if self.position and self.position_price is not None and self.last_price is not None else 0,
                "broker_position": self.broker_position,
                "reconcile_ok": self.reconcile_ok,
                "mismatch_min": self.mismatch_min,
                "order_contract": self.order_contract,
                "pending_order": self.pending_order,
                "kill": self.kill,
                "kill_reason": self.kill_reason,
                "strategy_halted": self.strategy_halted,
                "db": self.db,
                "telegram": __import__("app.notify", fromlist=["enabled"]).enabled(),
                "portfolio": __import__("app.portfolio", fromlist=["PORTFOLIO"]).PORTFOLIO.snapshot(),
                "now": now().isoformat(timespec="seconds"),
                "in_session": in_session(now()),
                "holiday": is_holiday(now().date()),
                "holidays": sorted(x.isoformat() for x in HOLIDAYS if x >= now().date())[:5],
                "session_note": self.session_note,
            }

    def recent_bars(self, n=60):
        with self.lock:
            return list(self.bars)[-n:]

    def recent_signals(self):
        with self.lock:
            return list(self.signals)

    def recent_events(self):
        with self.lock:
            return list(self.events)

    def recent_orders(self):
        with self.lock:
            return list(self.orders)


def session_remaining_min(dt):
    """距本時段收盤還有幾分鐘；非交易時段回 0。"""
    if not in_session(dt):
        return 0
    m = dt.hour * 60 + dt.minute
    if 8 * 60 + 45 <= m <= 13 * 60 + 45:
        return 13 * 60 + 45 - m
    return (5 * 60 - m) if m < 5 * 60 else (24 * 60 - m + 5 * 60)


import os as _os
from datetime import date as _date


def _parse_holidays():
    out = set()
    for x in _os.getenv("MARKET_HOLIDAYS", "").replace("，", ",").split(","):
        x = x.strip()
        if x:
            try:
                out.add(_date.fromisoformat(x))
            except ValueError:
                pass
    return out


ENV_HOLIDAYS = _parse_holidays()          # 來自 Zeabur 變數 MARKET_HOLIDAYS
SAVED_HOLIDAYS = set()                    # 來自儀表板設定（存 DB）
HOLIDAYS = set(ENV_HOLIDAYS)              # 生效中 = 兩者聯集；原地更新，勿重新指派


def parse_date_list(text):
    """接受換行、逗號、空白、頓號分隔；YYYY-MM-DD 或 YYYY/MM/DD。回 (日期集合, 無法解析的字串清單)。"""
    import re
    ok, bad = set(), []
    for tok in re.split(r"[\s,，、;；]+", str(text or "")):
        tok = tok.strip()
        if not tok:
            continue
        try:
            ok.add(_date.fromisoformat(tok.replace("/", "-")))
        except ValueError:
            bad.append(tok)
    return ok, bad


def set_saved_holidays(dates):
    SAVED_HOLIDAYS.clear()
    SAVED_HOLIDAYS.update(dates)
    HOLIDAYS.clear()
    HOLIDAYS.update(ENV_HOLIDAYS | SAVED_HOLIDAYS)


def is_holiday(d):
    return d in HOLIDAYS


def in_session(dt):
    """台指期交易時段：日盤 08:45–13:45，夜盤 15:00–翌日 05:00。"""
    if dt.weekday() >= 5 and not (dt.weekday() == 5 and dt.hour < 5):
        return False
    m = dt.hour * 60 + dt.minute
    if dt.weekday() == 0 and m < 5 * 60:     # 週一凌晨：週日沒有夜盤
        return False
    if HOLIDAYS:
        d = dt.date() if hasattr(dt, "date") else dt
        if is_holiday(d) and m >= 5 * 60:          # 假日當天的日盤與晚上
            return False
        prev = d.fromordinal(d.toordinal() - 1)
        if is_holiday(prev) and m < 5 * 60:        # 假日隔天凌晨（假日當晚沒有夜盤）
            return False
    if 8 * 60 + 45 <= m <= 13 * 60 + 45:
        return True
    if m >= 15 * 60 or m < 5 * 60:
        return True
    return False


STATE = State()
