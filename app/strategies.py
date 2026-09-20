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


class ARCrossoverShort(Strategy):
    """AR_crossover 的空方鏡像：L 下穿 MA(L)−AvgRange×ATRX 市價放空。

    與多方版完全對稱：進場條件、停損、停利、移動停利、結算日與週末出場都反向。
    台股長期偏多，空方鏡像的期望值通常低於多方，建議先用 paper 模式累積樣本再決定。
    """
    name = "AR_crossover_short"
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


REGISTRY = {c.name: c for c in (TMFF, ARCrossover2025, ARCrossoverShort, GuYuan2024, GuYuan2025, DemoMA)}
