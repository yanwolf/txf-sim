"""四支從 MultiCharts 移植的策略。參數在 strategy_config.json，這裡的 inputs 只是預設值。

每支的 on_bar() 逐行對照原 PowerLanguage，註解標原碼。
"""
from .el import Strategy, TICKSIZE


class TMFF(Strategy):
    """sa_a21TMFF：多空。均線±平均振幅過濾 + 日線三段連漲/連跌 + 前高/前低 stop 進場。"""
    name = "TMFF"
    desc = "多空。收盤站上/跌破 MA(N)±AvgRange(200)×倍數，且日線三段連漲/連跌，前高/前低 stop 進場"
    minutes = 60
    doc = dict(N="均線期數", BB="多方過濾：AvgRange(200) 的倍數", SS="空方過濾：AvgRange(200) 的倍數",
               SWing="SwingHigh/Low 左右各幾根", BLOSS="多單停損點數", BWIN="多單停利點數",
               SLOSS="空單停損點數", SWIN="空單停利點數", BMax="多單啟動移動停利的最大浮盈",
               BTB="多單移動停利回吐點數", SMax="空單啟動移動停利的最大浮盈", STB="空單移動停利回吐點數",
               TNw="週六幾點後不留單（HHMM）")
    inputs = dict(N=5, BB=50, SS=50, SWing=3, BLOSS=40, BWIN=500, SLOSS=40, SWIN=500,
                  BMax=50, BTB=100, SMax=50, STB=100, TNw=430)

    def __init__(self, cfg):
        super().__init__(cfg)
        self.BS = 0          # var:BS(0) 跨 K 棒保留
        self.winmax = 0.0

    def on_bar(self):
        p = self.p
        # IF closed(1)>closed(2) and closed(3)>closed(4) and closed(5)>closed(6) then BS=1;
        if self.closeD(1) > self.closeD(2) and self.closeD(3) > self.closeD(4) and self.closeD(5) > self.closeD(6):
            self.BS = 1
        if self.closeD(1) < self.closeD(2) and self.closeD(3) < self.closeD(4) and self.closeD(5) < self.closeD(6):
            self.BS = -1

        ma = self.average(self.C, p["N"])
        ar = self.avgrange(200)
        # IF MP<>1 and C>Average(C,N)+AvgRange(200)*BB and BS=1 then buy next bar SwingHigh(1,H,SWing,200) stop
        if self.mp != 1 and self.C() > ma + ar * p["BB"] and self.BS == 1:
            self.buy_stop(self.swinghigh(p["SWing"], 200), "SwingHigh 突破")
        # IF MP<>-1 and C<Average(C,N)-AvgRange(200)*SS and BS=-1 then sellshort next bar SwingLow stop
        if self.mp != -1 and self.C() < ma - ar * p["SS"] and self.BS == -1:
            self.sellshort_stop(self.swinglow(p["SWing"], 200), "SwingLow 跌破")

        if self.mp > 0:
            self.sell_stop(self.entryprice - TICKSIZE * p["BLOSS"], "停損")
            self.sell_limit(self.entryprice + TICKSIZE * p["BWIN"], "停利")
        if self.mp < 0:
            self.buytocover_stop(self.entryprice + TICKSIZE * p["SLOSS"], "停損")
            self.buytocover_limit(self.entryprice - TICKSIZE * p["SWIN"], "停利")

        # IF CheckDay then setexitonclose;
        if self.checkday:
            self.setexitonclose()

        # if MP<>0 then winmax=maxpositionprofit/bpv/contracts; if MP<>MP[1] then winmax=0;
        if self.mp != 0:
            self.winmax = self.maxprofit_pts
        if self.mp != self.mp_prev:
            self.winmax = 0
        if self.mp > 0 and self.winmax >= p["BMax"]:
            self.sell_stop(self.entryprice + self.winmax - p["BTB"], "移動停利")
        if self.mp < 0 and self.winmax >= p["SMax"]:
            self.buytocover_stop(self.entryprice - self.winmax + p["STB"], "移動停利")

        # 週末出場（原碼 T=TNw 的 stop 單在 60 分 K 永遠不觸發；改為週六 >= TNw 市價出場，不留單過週末）
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")


    def debug(self):
        p = self.p
        ma = self.average(self.C, p["N"])
        ar = self.avgrange(200)
        return {"收盤": self.C(), f"MA{int(p['N'])}": ma, "AvgRange(200)": ar,
                "多軌 MA+AR×BB": ma + ar * p["BB"], "空軌 MA−AR×SS": ma - ar * p["SS"],
                "BS 日線方向": self.BS, "SwingHigh": self.swinghigh(p["SWing"], 200),
                "SwingLow": self.swinglow(p["SWing"], 200),
                "多單條件": self.C() > ma + ar * p["BB"] and self.BS == 1,
                "空單條件": self.C() < ma - ar * p["SS"] and self.BS == -1,
                "日收(1~6)": [round(self.closeD(i), 1) for i in range(1, 7)],
                "日線(近5根)": [f"{d['date']} O{d['open']:.0f} H{d['high']:.0f} L{d['low']:.0f} C{d['close']:.0f}"
                              for d in self.days[-5:]]}


class ARCrossover2025(Strategy):
    """sb_b21AR_crossover_2025：只做多。H 上穿 MA(H)+AvgRange*ATRX 市價進場。"""
    name = "AR_crossover_2025"
    desc = "只做多。最高價上穿 MA(H)+AvgRange×倍數 市價進場，每日最多 ETD 次"
    minutes = 60
    doc = dict(MAN="最高價均線期數", ATRN="AvgRange 期數", ATRX="AvgRange 倍數", ETD="每日最多進場次數",
               TN="結算日幾點出場（HHMM）", LOSS="停損點數", WIN="停利點數", Twin="啟動移動停利的最大浮盈",
               Tstop="移動停利回吐點數", TNw="週六幾點後不留單（HHMM）")
    inputs = dict(MAN=20, ATRN=50, ATRX=1, ETD=2, TN=1245, LOSS=100, WIN=500, Twin=200, Tstop=200, TNw=430)

    def on_bar(self):
        p = self.p
        band0 = self.average(self.H, p["MAN"]) + self.avgrange(p["ATRN"]) * p["ATRX"]
        band1 = self.average(lambda k: self.H(k + 1), p["MAN"]) + \
            (sum(self.H(k + 1) - self.L(k + 1) for k in range(int(p["ATRN"]))) / int(p["ATRN"])) * p["ATRX"]
        crosses_over = self.H(1) <= band1 and self.H() > band0
        # IF EntriesToday(D)<ETD and H crosses over ... then buy next bar market;
        if self.entries_today < p["ETD"] and crosses_over:
            self.buy_market("H 上穿 MA+AR")

        value90 = self.maxprofit_pts
        if self.mp > 0:
            self.sell_stop(self.entryprice - TICKSIZE * p["LOSS"], "停損")
            self.sell_limit(self.entryprice + TICKSIZE * p["WIN"], "停利")
            if value90 >= p["Twin"] * TICKSIZE:
                self.sell_stop(self.entryprice + value90 - p["Tstop"] * TICKSIZE, "移動停利")

        # IF CheckDay and T=TN then sell next bar at market;
        if self.checkday and self.T() == p["TN"]:
            self.sell_market("結算日出場")
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")


    def debug(self):
        p = self.p
        band = self.average(self.H, p["MAN"]) + self.avgrange(p["ATRN"]) * p["ATRX"]
        return {"最高價": self.H(), "前一根最高": self.H(1), f"MA(H,{int(p['MAN'])})": self.average(self.H, p["MAN"]),
                f"AvgRange({int(p['ATRN'])})": self.avgrange(p["ATRN"]), "進場軌 MA+AR×ATRX": band,
                "今日進場次數": self.entries_today, "結算日": self.checkday}


class ARCrossunder2025(Strategy):
    """AR_crossover 的空方鏡像：L 下穿 MA(L)−AvgRange×ATRX 市價放空。

    與多方版完全對稱：進場條件、停損、停利、移動停利、結算日與週末出場都反向。
    台股長期偏多，空方鏡像的期望值通常低於多方，建議先用 paper 模式累積樣本再決定。
    """
    name = "AR_crossunder_2025"
    desc = "只做空。最低價下穿 MA(L)−AvgRange×倍數 市價放空，每日最多 ETD 次（AR_crossover 的鏡像）"
    minutes = 60
    inputs = dict(MAN=15, ATRN=35, ATRX=0.1, ETD=2, TN=1245, LOSS=55, WIN=500, Twin=200, Tstop=100, TNw=330)
    doc = dict(MAN="最低價均線期數", ATRN="AvgRange 期數", ATRX="AvgRange 倍數", ETD="每日最多進場次數",
               TN="結算日幾點出場（HHMM）", LOSS="停損點數", WIN="停利點數", Twin="啟動移動停利的最大浮盈",
               Tstop="移動停利回吐點數", TNw="週六幾點後不留單（HHMM）")

    def _band(self, shift=0):
        p = self.p
        ma = self.average(lambda k: self.L(k + shift), p["MAN"])
        ar = sum(self.H(k + shift) - self.L(k + shift) for k in range(int(p["ATRN"]))) / int(p["ATRN"])
        return ma - ar * p["ATRX"]

    def on_bar(self):
        p = self.p
        crosses_under = self.L(1) >= self._band(1) and self.L() < self._band(0)
        # IF EntriesToday(D)<ETD and L crosses under MA(L)-AvgRange*ATRX then sellshort next bar market;
        if self.entries_today < p["ETD"] and crosses_under:
            self.sellshort_market("L 下穿 MA−AR")

        value90 = self.maxprofit_pts
        if self.mp < 0:
            self.buytocover_stop(self.entryprice + TICKSIZE * p["LOSS"], "停損")
            self.buytocover_limit(self.entryprice - TICKSIZE * p["WIN"], "停利")
            if value90 >= p["Twin"] * TICKSIZE:
                self.buytocover_stop(self.entryprice - value90 + p["Tstop"] * TICKSIZE, "移動停利")

        if self.checkday and self.T() == p["TN"]:
            self.buytocover_market("結算日出場")
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")

    def debug(self):
        p = self.p
        return {"最低價": self.L(), "前一根最低": self.L(1), f"MA(L,{int(p['MAN'])})": self.average(self.L, p["MAN"]),
                f"AvgRange({int(p['ATRN'])})": self.avgrange(p["ATRN"]), "進場軌 MA−AR×ATRX": self._band(0),
                "今日進場次數": self.entries_today, "結算日": self.checkday}

class GuYuan2024(Strategy):
    """sb_b21GuYuan_2024：只做多。週高/時段收/區間中值合成價，N 根最高 stop 進場，N 根最低 stop 出場。"""
    name = "GuYuan_2024"
    desc = "只做多。(週高+時段收+區間中值)/3 合成價位，H 站上 → N 根最高 stop 進場；L 跌破 → N 根最低 stop 出場"
    minutes = 60
    doc = dict(N1="幾週前的週高/週低", N2="幾個時段前的收盤", N3="區間中值的期數", HH="進場：幾根最高價",
               LL="出場：幾根最低價", LOSS="停損點數", WIN="停利點數", Twin="啟動移動停利的最大浮盈",
               Tstop="移動停利回吐點數", TNw="週六幾點後不留單（HHMM）")
    inputs = dict(N1=1, N2=1, N3=1, HH=10, LL=10, LOSS=50, WIN=250, Twin=50, Tstop=200, TNw=430)

    def on_bar(self):
        p = self.p
        mid = (self.highest(self.C, p["N3"]) + self.lowest(self.C, p["N3"])) * 0.5
        value1 = (self.highW(p["N1"]) + self.closeS(p["N2"]) + mid) / 3
        value2 = (self.lowW(p["N1"]) + self.closeS(p["N2"]) + mid) / 3

        # IF MP<>1 and H>value1 then buy next bar at highest(H,HH) stop;
        if self.mp != 1 and self.H() > value1:
            self.buy_stop(self.highest(self.H, p["HH"]), "N 根最高突破")
        # IF MP=1 and L<value2 then sell next bar at lowest(L,LL) stop;
        if self.mp == 1 and self.L() < value2:
            self.sell_stop(self.lowest(self.L, p["LL"]), "N 根最低跌破")

        if self.mp > 0:
            self.sell_stop(self.entryprice - TICKSIZE * p["LOSS"], "停損")
            self.sell_limit(self.entryprice + TICKSIZE * p["WIN"], "停利")

        v1 = self.maxprofit_pts
        if self.mp > 0 and v1 > p["Twin"] * TICKSIZE:
            self.sell_stop(self.entryprice + v1 - p["Tstop"] * TICKSIZE, "移動停利")

        if self.checkday:
            self.setexitonclose()
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")


    def debug(self):
        p = self.p
        mid = (self.highest(self.C, p["N3"]) + self.lowest(self.C, p["N3"])) * 0.5
        v1 = (self.highW(p["N1"]) + self.closeS(p["N2"]) + mid) / 3
        v2 = (self.lowW(p["N1"]) + self.closeS(p["N2"]) + mid) / 3
        return {"最高價": self.H(), "最低價": self.L(), f"週高({int(p['N1'])})": self.highW(p["N1"]),
                f"週低({int(p['N1'])})": self.lowW(p["N1"]), f"時段收({int(p['N2'])})": self.closeS(p["N2"]),
                "區間中值": mid, "進場價 value1": v1, "出場價 value2": v2,
                f"最高({int(p['HH'])}根)": self.highest(self.H, p["HH"]), f"最低({int(p['LL'])}根)": self.lowest(self.L, p["LL"]),
                "結算日": self.checkday}


class GuYuan2025(Strategy):
    """sb_b21GuYuan_2025：只做多。20 分 K，加均線多頭排列與每日進場次數限制。"""
    name = "GuYuan_2025"
    desc = "只做多。合成價位 + 均線多頭排列（MA1>MA2），N 根最高 stop 進場，每日最多 ETD 次"
    minutes = 20
    doc = dict(HN="進場：幾根最高價", N1="幾週前的週高（0=本週）", N2="幾天前的日收盤（0=今天）",
               N3="區間中值的期數", MA1="短均線期數", MA2="長均線期數", ETD="每日最多進場次數",
               TN="結算日幾點出場（HHMM）", LOSS="停損點數", WIN="停利點數", TNw="週六幾點後不留單（HHMM）")
    inputs = dict(HN=20, N1=0, N2=0, N3=0, MA1=1, MA2=1, ETD=5, TN=1325, LOSS=100, WIN=500, TNw=430)

    def on_bar(self):
        p = self.p
        mid = (self.highest(self.C, p["N3"]) + self.lowest(self.C, p["N3"])) * 0.5
        value1 = (self.highW(p["N1"]) + self.closeD(p["N2"]) + mid) / 3

        # IF MP<>1 and EntriesToday(D)<ETD and H>value1 and Averagefc(C,ma1)>Averagefc(C,ma2) and ma1<ma2
        if self.mp != 1 and self.entries_today < p["ETD"] and self.H() > value1 \
                and self.average(self.C, p["MA1"]) > self.average(self.C, p["MA2"]) and p["MA1"] < p["MA2"]:
            self.buy_stop(self.highest(self.H, p["HN"]), "N 根最高突破")

        if self.mp > 0:
            self.sell_stop(self.entryprice - TICKSIZE * p["LOSS"], "停損")
            self.sell_limit(self.entryprice + TICKSIZE * p["WIN"], "停利")

        if self.checkday and self.T() == p["TN"]:
            self.sell_market("結算日出場")
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")


    def debug(self):
        p = self.p
        mid = (self.highest(self.C, p["N3"]) + self.lowest(self.C, p["N3"])) * 0.5
        v1 = (self.highW(p["N1"]) + self.closeD(p["N2"]) + mid) / 3
        return {"最高價": self.H(), f"週高({int(p['N1'])})": self.highW(p["N1"]),
                f"日收({int(p['N2'])})": self.closeD(p["N2"]), "區間中值": mid, "進場價 value1": v1,
                f"MA{int(p['MA1'])}": self.average(self.C, p["MA1"]), f"MA{int(p['MA2'])}": self.average(self.C, p["MA2"]),
                "均線多頭": self.average(self.C, p["MA1"]) > self.average(self.C, p["MA2"]),
                f"最高({int(p['HN'])}根)": self.highest(self.H, p["HN"]),
                "今日進場次數": self.entries_today, "結算日": self.checkday}


# ⚠️ 指標參數（LADDER/OPENRULE/OPENVOL/AVL_RESET/MA1/MA2）已校準鎖定，見 ORBIT_HANDOFF.md；改進請加新參數，預設維持現行行為
class OrbitCost30(Strategy):
    """軌道鞅 v1：30 分 K 多空成本（大量紅黑 K 階梯）+ 大小流氓濾網 + 公道伯方向。

    進場（ENTRY：1=回測有守、2=頂底被破、3=兩者都用）
      多：大小流氓（MA1、MA2）之下不做多；價在公道伯之上（USE_AVL=1）
          1) 回測多方階梯（低點碰到 階梯+TOUCH 內）且收盤守住 → 下一根市價進多
          2) 收盤由下往上突破空方階梯（大黑頂被破）→ 下一根市價進多
      空：完全鏡像
    出場
      停損：進場參考階梯 −BUF（多單），之後出現更高的多方階梯就往上移；另有 MAXLOSS 上限
      停利：滿足點 TP_PCT %（0 = 不設）
      EXIT_OPP=1：出現反方向大量 K 就市價出場
      結算日收盤出場、週六 TNw 後出場
    參數與 App 對照、校準方式見 orbit.py 註解。預設 paper 模式，不下單。
    """
    name = "Orbit_cost30"
    version = "v1.1"          # v1.1：修正反手沿用舊停損（2026-09-30）
    desc = "軌道鞅 v1：30 分 K 大量紅黑 K 階梯（多空成本）+ 大小流氓濾網 + 公道伯，回測有守/頂底被破進場"
    minutes = 30
    doc = dict(LADDER="階梯算法：0=開盤法（對照 App）、1=量倍數法", AVL_RESET="公道伯歸零：0=結算日收盤後、1=每月、2=結算日開盤", OPENVOL="開盤法量門檻（口，0=不限）", OPENRULE="開盤法判定：0=只看收盤、1=跳空確認",
               VOLN="大量判定：同時段近幾根均量", VOLX="大量判定：均量倍數", BODY="大量 K 最小實體（點）",
               DAYONLY="1=一般盤（只看日盤）、0=合併盤", MA1="小流氓均線期數", MA2="大流氓均線期數",
               USE_AVL="1=用公道伯過濾方向", SIDE="0=多空、1=只做多、-1=只做空",
               ENTRY="1=回測有守、2=頂底被破、3=兩者", TOUCH="回測判定：離階梯幾點內算碰到",
               BUF="停損：階梯外再留幾點", MAXLOSS="單筆最大停損點數", TP_PCT="滿足點停利（%，0=不設）",
               EXIT_OPP="1=反向大量 K 出場", ETD="每日最多進場次數", TNw="週六幾點後不留單（HHMM）")
    inputs = dict(LADDER=0, AVL_RESET=2, OPENVOL=0, OPENRULE=1, VOLN=20, VOLX=2.0, BODY=0, DAYONLY=0, MA1=20, MA2=40, USE_AVL=1, SIDE=0, ENTRY=3,
                  TOUCH=20, BUF=10, MAXLOSS=150, TP_PCT=1.2, EXIT_OPP=1, ETD=2, TNw=330)

    def __init__(self, cfg):
        super().__init__(cfg)
        from .orbit import OrbitCalc
        self.calc = OrbitCalc(self.p)
        self.pos_stop = None
        self.pending_ref = None
        self.stop_moved = False                   # 只影響出場標籤（原始停損 vs 已移動），不影響行為

    def on_bar(self):
        p, c = self.p, self.calc
        i = len(self.bars) - 1                    # 補上還沒餵過的 K（重啟回放時可能一次給很多根）
        while i >= 0 and (c.last_ts is None or self.bars[i]["ts"] > c.last_ts):
            i -= 1
        for bb in self.bars[i + 1:]:
            c.update(bb, self.trading_days)
        if c.ma2 is None:
            return
        b = self.bars[self._i]
        C, L, H = self.C(), self.L(), self.H()
        side = int(p["SIDE"])
        entry = int(p["ENTRY"])
        day_ok = b["is_day"] or not int(p["DAYONLY"])
        bull = C > c.ma1 and C > c.ma2
        bear = C < c.ma1 and C < c.ma2
        if int(p["USE_AVL"]) and c.avl:
            bull = bull and C > c.avl
            bear = bear and C < c.avl

        if self.mp != self.mp_prev or self.mp == 0:
            if self.mp == 0:
                self.pos_stop = None
                self.stop_moved = False
            elif self.mp_prev != 0 and (self.mp > 0) != (self.mp_prev > 0):
                # 反手（多翻空／空翻多）：舊方向的停損價不能沿用，改用新部位的 pending_ref 重算
                self.pos_stop = None
                self.stop_moved = False

        # ---- 進場
        can = day_ok and self.entries_today < p["ETD"]
        if can and self.mp <= 0 and side >= 0 and bull:
            if entry in (1, 3) and c.long_ladder and L <= c.long_ladder + p["TOUCH"] and C > c.long_ladder:
                self.pending_ref = c.long_ladder
                self.buy_market("回測多方階梯有守")
            elif entry in (2, 3) and c.short_ladder and self.C(1) <= c.short_ladder < C and c.big != -1:
                self.pending_ref = c.short_ladder
                self.buy_market("大黑頂被破")
        if can and self.mp >= 0 and side <= 0 and bear:
            if entry in (1, 3) and c.short_ladder and H >= c.short_ladder - p["TOUCH"] and C < c.short_ladder:
                self.pending_ref = c.short_ladder
                self.sellshort_market("回測空方階梯有守")
            elif entry in (2, 3) and c.long_ladder and self.C(1) >= c.long_ladder > C and c.big != 1:
                self.pending_ref = c.long_ladder
                self.sellshort_market("大紅底被破")

        # ---- 出場
        if self.mp > 0 and self.entryprice is not None:
            if self.pos_stop is None:
                ref = self.pending_ref if self.pending_ref else self.entryprice - p["MAXLOSS"]
                self.pos_stop = max(ref - p["BUF"], self.entryprice - p["MAXLOSS"])
            if c.long_ladder and c.long_ladder < C and c.long_ladder - p["BUF"] > self.pos_stop:
                self.pos_stop = c.long_ladder - p["BUF"]          # 停利點往上設
                self.stop_moved = True
            self.sell_stop(self.pos_stop, "階梯停損（已上移）" if self.stop_moved else "階梯停損（原始）")
            if p["TP_PCT"]:
                self.sell_limit(self.entryprice * (1 + p["TP_PCT"] / 100), "滿足點")
            if int(p["EXIT_OPP"]) and c.big == -1:
                self.sell_market("空方大量出現")
        if self.mp < 0 and self.entryprice is not None:
            if self.pos_stop is None:
                ref = self.pending_ref if self.pending_ref else self.entryprice + p["MAXLOSS"]
                self.pos_stop = min(ref + p["BUF"], self.entryprice + p["MAXLOSS"])
            if c.short_ladder and c.short_ladder > C and c.short_ladder + p["BUF"] < self.pos_stop:
                self.pos_stop = c.short_ladder + p["BUF"]         # 停利點往下設
                self.stop_moved = True
            self.buytocover_stop(self.pos_stop, "階梯停損（已下移）" if self.stop_moved else "階梯停損（原始）")
            if p["TP_PCT"]:
                self.buytocover_limit(self.entryprice * (1 - p["TP_PCT"] / 100), "滿足點")
            if int(p["EXIT_OPP"]) and c.big == 1:
                self.buytocover_market("多方大量出現")

        if self.checkday:
            self.setexitonclose()
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")

    def debug(self):
        c = self.calc
        r1 = lambda x: round(x, 1) if x is not None else None
        return {"收盤": self.C(), "多方階梯": r1(c.long_ladder), "多方階梯時間": c.long_ts,
                "空方階梯": r1(c.short_ladder), "空方階梯時間": c.short_ts,
                f"MA{int(self.p['MA1'])}": r1(c.ma1), f"MA{int(self.p['MA2'])}": r1(c.ma2),
                "公道伯 AVL": r1(c.avl), "本根大量": {1: "紅", -1: "黑", 0: "否"}[c.big],
                "持倉停損價": r1(self.pos_stop), "今日進場次數": self.entries_today}


class OrbitGap(Strategy):
    """軌道鞅 v3：日 K 奇襲缺口（奇襲方擁有發球權，回測奇襲防守點有守進場）。

    日 K 在策略內部用已收盤的 N 分 K 逐根合成，不讀引擎的 days（那份含未來資料）。
      DAYMODE 1=只用日盤 08:45–13:45 組日 K；0=前一晚夜盤＋當天日盤合併（同期交所交易日）
    缺口在「日 K 收盤」時確認，隔天起才可進場（不偷看當天）：
      GAP_MODE 1=真缺口：今低 > 昨高（空：今高 < 昨低），防守點＝昨高（昨低）
               2=實體缺口：今日實體完全在昨日實體之上（下），防守點＝昨日實體上緣（下緣）
               3=開盤跳空：今開、今收都在昨收之上（下），防守點＝昨收（v1 開盤法的日 K 版）
      GAP_MIN：缺口至少幾點（今日對應價與防守點的距離）
    缺口自成一局：只追蹤最新一個缺口；新缺口（任一方向）出現就取代舊的。
    缺口失效：收盤跌破（多）/ 站上（空）防守點 ±BUF 視為回補；GAP_DAYS>0 時超過幾個日 K 也失效。
    進場：多方缺口有效時，K 低點回測到 防守點+TOUCH 內且收盤守住 → 下一根市價進多（空方鏡像）
    出場：停損 防守點−BUF（上限 MAXLOSS）；滿足點 TP_PCT %；EXIT_OPP=1 出現反向缺口就出場；
          結算日收盤、週六 TNw 後出場（與 v1 相同）
    USE_AVL=1 時加公道伯方向濾網（直接用 v1 的 OrbitCalc，不另寫）。預設 paper 模式，不下單。
    """
    name = "Orbit_gap"
    version = "v3.0"
    desc = "軌道鞅 v3：日 K 奇襲缺口，回測防守點有守進場（缺口定義、日 K 時段可切換）"
    minutes = 30
    doc = dict(DAYMODE="日 K 時段：1=只用日盤、0=夜盤＋日盤合併",
               GAP_MODE="缺口定義：1=真缺口（今低>昨高）、2=實體缺口、3=開盤跳空（開收都在昨收外）",
               GAP_MIN="缺口最小點數（0=不限）", GAP_DAYS="缺口有效日 K 數（0=直到回補或被新缺口取代）",
               USE_AVL="1=加公道伯方向濾網", SIDE="0=多空、1=只做多、-1=只做空",
               TOUCH="回測判定：離防守點幾點內算碰到", BUF="停損與回補判定：防守點外再留幾點",
               MAXLOSS="單筆最大停損點數", TP_PCT="滿足點停利（%，0=不設）",
               EXIT_OPP="1=出現反向缺口就出場", ETD="每日最多進場次數", TNw="週六幾點後不留單（HHMM）")
    inputs = dict(DAYMODE=1, GAP_MODE=1, GAP_MIN=0, GAP_DAYS=0, USE_AVL=0, SIDE=0,
                  TOUCH=20, BUF=10, MAXLOSS=150, TP_PCT=1.2, EXIT_OPP=1, ETD=1, TNw=330)

    def __init__(self, cfg):
        super().__init__(cfg)
        if "mode" not in cfg:
            self.mode = "paper"               # 新策略沒設定時一律 paper，避免誤下單
        self.cur_day = None                   # 正在累積的日 K
        self.prev_day = None                  # 上一根已收盤日 K
        self.days_done = 0
        self.last_fed = None
        self.gap = None                       # {"dir":1/-1, "def":防守點, "far":缺口另一端, "date":, "age":}
        self.new_gap_dir = 0                  # 這根 K 剛確認的缺口方向（給 EXIT_OPP）
        self.pos_stop = None
        self.pos_ref = None
        self.avl_calc = None
        if int(self.p.get("USE_AVL", 0)):
            from .orbit import OrbitCalc
            self.avl_calc = OrbitCalc(dict(OrbitCost30.inputs))   # 公道伯用 v1 已校準的預設參數

    # ---- 日 K 合成
    def _day_key(self, b):
        """回傳這根 K 所屬的日 K 鍵；DAYMODE=1 時夜盤回 None（不納入）。"""
        ses = b["session"]
        if ses.endswith("D"):
            return ses[:10]
        if int(self.p["DAYMODE"]):
            return None
        return "N" + ses[:10]                 # 夜盤暫用自己的鍵，遇到隔天日盤時合併

    def _close_day(self):
        d = self.cur_day
        self.cur_day = None
        if d is None:
            return
        self.new_gap_dir = 0
        pv = self.prev_day
        self.prev_day = d
        self.days_done += 1
        if self.gap is not None:
            self.gap["age"] += 1
            if int(self.p["GAP_DAYS"]) and self.gap["age"] > int(self.p["GAP_DAYS"]):
                self.gap = None
        if pv is None:
            return
        mode, gmin = int(self.p["GAP_MODE"]), float(self.p["GAP_MIN"])
        up = dn = None
        if mode == 1:
            if d["low"] > pv["high"] and d["low"] - pv["high"] >= gmin:
                up = (pv["high"], d["low"])
            if d["high"] < pv["low"] and pv["low"] - d["high"] >= gmin:
                dn = (pv["low"], d["high"])
        elif mode == 2:
            pt, pb = max(pv["open"], pv["close"]), min(pv["open"], pv["close"])
            tt, tb = max(d["open"], d["close"]), min(d["open"], d["close"])
            if tb > pt and tb - pt >= gmin:
                up = (pt, tb)
            if tt < pb and pb - tt >= gmin:
                dn = (pb, tt)
        else:
            pc = pv["close"]
            if d["open"] > pc and d["close"] > pc and d["open"] - pc >= gmin:
                up = (pc, d["open"])
            if d["open"] < pc and d["close"] < pc and pc - d["open"] >= gmin:
                dn = (pc, d["open"])
        g = up or dn
        if g:
            direction = 1 if up else -1
            self.gap = {"dir": direction, "def": g[0], "far": g[1], "date": d["key"], "age": 0}
            self.new_gap_dir = direction

    def _feed(self, b):
        key = self._day_key(b)
        if key is None:
            return
        if self.cur_day is not None and self.cur_day["key"] != key:
            merge = (key[0] != "N" and self.cur_day["key"].startswith("N") and
                     __import__("datetime").date.fromisoformat(self.cur_day["key"][1:]) +
                     __import__("datetime").timedelta(days=1) == __import__("datetime").date.fromisoformat(key))
            if merge:
                self.cur_day["key"] = key     # 前一晚夜盤併入今天日盤
            else:
                self._close_day()
        if self.cur_day is None:
            self.cur_day = {"key": key, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]}
        else:
            d = self.cur_day
            d["high"] = max(d["high"], b["high"]); d["low"] = min(d["low"], b["low"]); d["close"] = b["close"]
        if b.get("is_day_last") and key[0] != "N":
            self._close_day()                 # 日盤最後一根收盤＝日 K 收盤

    # ---- 策略
    def on_bar(self):
        p = self.p
        i = len(self.bars) - 1
        while i >= 0 and (self.last_fed is None or self.bars[i]["ts"] > self.last_fed):
            i -= 1
        self.new_gap_dir = 0
        for bb in self.bars[i + 1:]:
            self._feed(bb)
            if self.avl_calc:
                self.avl_calc.update(bb, self.trading_days)
            self.last_fed = bb["ts"]
        if self.mp == 0 or (self.mp_prev != 0 and (self.mp > 0) != (self.mp_prev > 0)):
            self.pos_stop = None              # 空手或反手：停損價重算（v1.1 的教訓）
        C, L, H = self.C(), self.L(), self.H()
        side = int(p["SIDE"])
        g = self.gap

        # 缺口回補 → 失效
        if g and ((g["dir"] > 0 and C < g["def"] - p["BUF"]) or (g["dir"] < 0 and C > g["def"] + p["BUF"])):
            self.gap = g = None

        avl = self.avl_calc.avl if self.avl_calc else None
        long_ok = avl is None or C > avl
        short_ok = avl is None or C < avl

        # ---- 進場
        if g and self.entries_today < p["ETD"]:
            if g["dir"] > 0 and self.mp <= 0 and side >= 0 and long_ok and \
                    L <= g["def"] + p["TOUCH"] and C > g["def"]:
                self.pos_ref = g["def"]
                self.buy_market("回測多方缺口有守")
            elif g["dir"] < 0 and self.mp >= 0 and side <= 0 and short_ok and \
                    H >= g["def"] - p["TOUCH"] and C < g["def"]:
                self.pos_ref = g["def"]
                self.sellshort_market("回測空方缺口有守")

        # ---- 出場
        if self.mp > 0 and self.entryprice is not None:
            if self.pos_stop is None:
                ref = self.pos_ref or self.entryprice - p["MAXLOSS"]
                self.pos_stop = max(ref - p["BUF"], self.entryprice - p["MAXLOSS"])
            self.sell_stop(self.pos_stop, "缺口停損")
            if p["TP_PCT"]:
                self.sell_limit(self.entryprice * (1 + p["TP_PCT"] / 100), "滿足點")
            if int(p["EXIT_OPP"]) and self.new_gap_dir == -1:
                self.sell_market("反向缺口出現")
        if self.mp < 0 and self.entryprice is not None:
            if self.pos_stop is None:
                ref = self.pos_ref or self.entryprice + p["MAXLOSS"]
                self.pos_stop = min(ref + p["BUF"], self.entryprice + p["MAXLOSS"])
            self.buytocover_stop(self.pos_stop, "缺口停損")
            if p["TP_PCT"]:
                self.buytocover_limit(self.entryprice * (1 - p["TP_PCT"] / 100), "滿足點")
            if int(p["EXIT_OPP"]) and self.new_gap_dir == 1:
                self.buytocover_market("反向缺口出現")

        if self.checkday:
            self.setexitonclose()
        if self.weekend_exit_due(p["TNw"]):
            self.exit_market("週末出場")

    def debug(self):
        g = self.gap
        r1 = lambda x: round(x, 1) if x is not None else None
        return {"收盤": self.C(), "日K數": self.days_done,
                "缺口方向": {1: "多", -1: "空"}.get(g["dir"]) if g else "無",
                "防守點": r1(g["def"]) if g else None, "缺口另一端": r1(g["far"]) if g else None,
                "缺口日": g["date"] if g else None, "缺口已過日K": g["age"] if g else None,
                "持倉停損價": r1(self.pos_stop), "今日進場次數": self.entries_today}


class DemoMA(Strategy):
    """示範：1 分 K 均線交叉（之前那支），預設關閉。"""
    name = "demo_ma"
    desc = "示範：1 分 K 均線交叉，多空"
    minutes = 1
    doc = dict(FAST="短均線", SLOW="長均線")
    inputs = dict(FAST=5, SLOW=20)

    def on_bar(self):
        p = self.p
        if self._i < p["SLOW"] + 1:
            return
        f0, s0 = self.average(self.C, p["FAST"]), self.average(self.C, p["SLOW"])
        f1 = self.average(lambda k: self.C(k + 1), p["FAST"])
        s1 = self.average(lambda k: self.C(k + 1), p["SLOW"])
        if f1 <= s1 and f0 > s0 and self.mp <= 0:
            self.buy_market("MA 上穿")
        if f1 >= s1 and f0 < s0 and self.mp >= 0:
            self.sellshort_market("MA 下穿")


REGISTRY = {c.name: c for c in (TMFF, ARCrossover2025, ARCrossunder2025, GuYuan2024, GuYuan2025,
                                  OrbitCost30, OrbitGap, DemoMA)}
