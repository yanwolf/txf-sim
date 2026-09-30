"""軌道鞅指標：多空成本（30 分 K 大量紅黑 K 階梯）、大小流氓、公道伯。

策略（strategies.OrbitCost30）和圖表 API（/api/chart）共用這一份計算，
所以圖上畫的線就是策略判斷時看到的線。

注意：這些是依課程定義「近似」的算法，App 的原始公式不公開。
請用圖表頁把數值跟 App 截圖對照，差太多就調參數（VOLN、VOLX、DAYONLY）。

  LADDER=0（開盤法，預設，對照 App 截圖反推）
    每個時段（日盤/夜盤）第一根 K 收盤後，跟「前一時段最後收盤價」比：
      收盤較高 → 標紅，多方階梯 = 前一時段收盤價
      收盤較低 → 標綠，空方階梯 = 前一時段收盤價
    也就是課程的「從哪裡攻擊就從哪裡算起」：階梯是這波攻擊的起點。
    驗證：9/29 日盤首根 → 多方階梯 47956（= 9/25 05:00 收盤），
          9/29 夜盤首根 → 多方階梯 47767（= 9/29 13:45 收盤），與 App 完全相同。
  LADDER=1（量倍數法，舊版）
    成交量 ≥ 同類時段近 VOLN 根均量 × VOLX 的紅/黑 K，取其低點/高點當階梯
  大小流氓   ：MA1、MA2（30 分 K 的 20MA、40MA；40MA ≈ 60 分 K 的 20MA）
  公道伯 AVL ：成交量加權均價。AVL_RESET=0 結算日日盤收盤後歸零；1 = 每月第一根 K 歸零
  DAYONLY=1  ：一般盤（只用日盤 K 計算大量與階梯）；0 = 合併盤
"""
from collections import deque

from . import tf


class OrbitCalc:
    def __init__(self, p):
        self.p = p
        self.last_ts = None
        self.vol_day = deque(maxlen=int(p.get("VOLN", 20)))
        self.vol_night = deque(maxlen=int(p.get("VOLN", 20)))
        self.closes = deque(maxlen=max(int(p.get("MA1", 20)), int(p.get("MA2", 40))))
        self.long_ladder = None       # 多方階梯（大紅 K 底）
        self.short_ladder = None      # 空方階梯（大黑 K 頂）
        self.long_ts = self.short_ts = None
        self.pv = 0.0                 # 公道伯累積 價×量
        self.vv = 0.0
        self.avl_reset_next = False
        self.big = 0                  # 這根是否標色：1 紅 / -1 黑 / 0
        self.ma1 = self.ma2 = self.avl = None
        self.cur_sess = None          # 開盤法：目前時段
        self.ref_close = None         # 開盤法：上一根（納入計算的）K 收盤
        self.last_month = None

    def update(self, b, trading_days):
        """餵一根已收盤的 N 分 K。同一根重複餵會被忽略。"""
        if self.last_ts is not None and b["ts"] <= self.last_ts:
            return False
        self.last_ts = b["ts"]
        p = self.p

        # 大小流氓（所有 K 棒都算，合併盤/一般盤都一樣）
        self.closes.append(b["close"])
        n1, n2 = int(p.get("MA1", 20)), int(p.get("MA2", 40))
        cs = list(self.closes)
        self.ma1 = sum(cs[-n1:]) / n1 if len(cs) >= n1 else None
        self.ma2 = sum(cs[-n2:]) / n2 if len(cs) >= n2 else None

        # 公道伯：結算日日盤收盤後的下一根開始歸零
        mon = (b["date"].year, b["date"].month)
        if int(p.get("AVL_RESET", 0)) == 1:
            if self.last_month is not None and mon != self.last_month:
                self.pv = self.vv = 0.0
            self.avl_reset_next = False
        elif self.avl_reset_next:
            self.pv = self.vv = 0.0
            self.avl_reset_next = False
        self.last_month = mon
        v = b.get("volume") or 0
        tp = (b["high"] + b["low"] + b["close"]) / 3
        self.pv += tp * v
        self.vv += v
        self.avl = self.pv / self.vv if self.vv > 0 else None
        if b["is_day"] and b.get("is_day_last") and tf.settlement_day(b["date"], trading_days):
            self.avl_reset_next = True

        # 大量紅黑 K 與階梯
        self.big = 0
        use = b["is_day"] or not int(p.get("DAYONLY", 0))
        if use and int(p.get("LADDER", 0)) == 0:
            if b["session"] != self.cur_sess:            # 時段第一根
                ref = self.ref_close
                if ref is not None:
                    if b["close"] > ref:
                        self.big = 1
                        self.long_ladder, self.long_ts = ref, b["ts"]
                    elif b["close"] < ref:
                        self.big = -1
                        self.short_ladder, self.short_ts = ref, b["ts"]
                self.cur_sess = b["session"]
            self.ref_close = b["close"]
        elif use:
            q = self.vol_day if b["is_day"] else self.vol_night
            n = q.maxlen
            if len(q) >= max(5, n // 2):
                avg = sum(q) / len(q)
                body = abs(b["close"] - b["open"])
                if avg > 0 and v >= avg * float(p.get("VOLX", 2.0)) and body >= float(p.get("BODY", 0)):
                    if b["close"] > b["open"]:
                        self.big = 1
                        self.long_ladder, self.long_ts = b["low"], b["ts"]
                    elif b["close"] < b["open"]:
                        self.big = -1
                        self.short_ladder, self.short_ts = b["high"], b["ts"]
            q.append(v)
        return True

    def row(self, b):
        r1 = lambda x: round(x, 1) if x is not None else None
        return {"ts": b["ts"], "long": r1(self.long_ladder), "short": r1(self.short_ladder),
                "ma1": r1(self.ma1), "ma2": r1(self.ma2), "avl": r1(self.avl), "big": self.big}


def series(bars, p):
    """整段 K 棒跑一次，回每根的指標值（給圖表用）。"""
    trading_days = {b["date"] for b in bars if b["is_day"]}
    c = OrbitCalc(p)
    out = []
    for b in bars:
        c.update(b, trading_days)
        out.append(c.row(b))
    return out
