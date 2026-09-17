"""示範策略：1 分 K 均線交叉，只產生訊號、不下單。

之後把 MultiCharts 的策略邏輯搬過來時，只要換掉 on_bar_close() 內容即可。
每根 1 分 K 收完後會被呼叫一次，bars 是由舊到新的 list。
"""
import os

from .broker import BROKER
from .state import STATE

FAST = int(os.getenv("FAST_MA", "5"))
SLOW = int(os.getenv("SLOW_MA", "20"))


def _ma(bars, n):
    if len(bars) < n:
        return None
    return sum(b["close"] for b in bars[-n:]) / n


def on_bar_close(bars):
    if len(bars) < SLOW + 1:
        return

    fast_now, slow_now = _ma(bars, FAST), _ma(bars, SLOW)
    fast_prev, slow_prev = _ma(bars[:-1], FAST), _ma(bars[:-1], SLOW)
    price = bars[-1]["close"]

    golden = fast_prev <= slow_prev and fast_now > slow_now
    death = fast_prev >= slow_prev and fast_now < slow_now

    with STATE.lock:
        pos = STATE.position

    # 只需要告訴下單層目標方向：+1 多 / -1 空 / 0 空手
    if golden and pos <= 0:
        BROKER.set_target(+1, price, f"MA{FAST} 上穿 MA{SLOW}")
    elif death and pos >= 0:
        BROKER.set_target(-1, price, f"MA{FAST} 下穿 MA{SLOW}")
