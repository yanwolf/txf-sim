"""四支從 MultiCharts 移植的策略。參數在 strategy_config.json，這裡的 inputs 只是預設值。

每支的 on_bar() 逐行對照原 PowerLanguage，註解標原碼。
"""
from .el import Strategy, TICKSIZE


class TMFF(Strategy):
    """sa_a21TMFF：多空。均線±平均振幅過濾 + 日線三段連漲/連跌 + 前高/前低 stop 進場。"""
    name = "TMFF"
    minutes = 60
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


class ARCrossover2025(Strategy):
    """sb_b21AR_crossover_2025：只做多。H 上穿 MA(H)+AvgRange*ATRX 市價進場。"""
    name = "AR_crossover_2025"
    minutes = 60
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


class GuYuan2024(Strategy):
    """sb_b21GuYuan_2024：只做多。週高/時段收/區間中值合成價，N 根最高 stop 進場，N 根最低 stop 出場。"""
    name = "GuYuan_2024"
    minutes = 60
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


class GuYuan2025(Strategy):
    """sb_b21GuYuan_2025：只做多。20 分 K，加均線多頭排列與每日進場次數限制。"""
    name = "GuYuan_2025"
    minutes = 20
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


class DemoMA(Strategy):
    """示範：1 分 K 均線交叉（之前那支），預設關閉。"""
    name = "demo_ma"
    minutes = 1
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


REGISTRY = {c.name: c for c in (TMFF, ARCrossover2025, GuYuan2024, GuYuan2025, DemoMA)}
