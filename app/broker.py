"""下單層：策略只說「我要多/空/空手」，這裡負責把部位變成那樣。

MODE:
  signal  只記錄訊號與虛擬部位（預設）
  sim     真的送單到 Shioaji 模擬環境（SIMULATION=true）
  live    正式下單，需 SIMULATION=false、LIVE_CONFIRM=YES、憑證

狀態機：sent → filled / cancelled / failed
  - IOC 市價單，正常幾百毫秒內成交或取消
  - 超過 ORDER_TIMEOUT 秒沒回報就主動查狀態
  - 取消（沒成交）重送一次；連續失敗達 MAX_ORDER_FAILURES 就停策略等人看
風控：kill switch、單日虧損上限、最大口數、非交易時段不送單、可選日盤收盤前平倉
對帳：每分鐘拿券商部位比對內部部位，不一致就告警並以券商為準
"""
import os
import threading
import time
import uuid

import shioaji as sj

from .db import DB_
from .notify import notify
from .state import STATE, in_session, now

MODE = os.getenv("MODE", "signal").lower()
LOTS = int(os.getenv("LOTS", "1"))
MAX_POSITION = int(os.getenv("MAX_POSITION", "1"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT_PTS", "300"))   # 每口點數，0 = 關閉
ORDER_TIMEOUT = int(os.getenv("ORDER_TIMEOUT", "15"))
MAX_ORDER_FAILURES = int(os.getenv("MAX_ORDER_FAILURES", "2"))
FLAT_AT_DAY_CLOSE = os.getenv("FLAT_AT_DAY_CLOSE", "false").lower() == "true"
RECONCILE_ADOPT = os.getenv("RECONCILE_ADOPT", "true").lower() == "true"

# 新舊版 shioaji 常數相容
_C = sj.constant
Action = getattr(sj, "Action", None) or _C.Action
FPT = getattr(sj, "FuturesPriceType", None) or _C.FuturesPriceType
OT = getattr(sj, "OrderType", None) or getattr(_C, "OrderType", None) or _C.FuturesOrderType
OC = getattr(sj, "FuturesOCType", None) or _C.FuturesOCType
FuturesOrder = getattr(sj, "FuturesOrder", None) or sj.Order


def _ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")


class Broker:
    def __init__(self):
        self.lock = threading.RLock()
        self.api = None
        self.contract = None       # 實際月合約
        self.account = None
        self.orders = {}           # id -> dict
        self.pending = None        # 目前未完成的 order id
        self.failures = 0
        self.day = None
        self._seen_deals = set()
        self._last_risk_check = 0.0
        self._last_reconcile = 0.0
        STATE.mode = MODE

    # ------------------------------------------------------------ 初始化
    def restore(self):
        """從 DB 讀回部位與風控狀態（容器重啟用）。"""
        kv = DB_.get_kv("broker") or {}
        with STATE.lock, self.lock:
            STATE.position = int(kv.get("position", 0))
            STATE.position_price = kv.get("position_price")
            STATE.realized_pnl = float(kv.get("realized_pnl", 0.0))
            STATE.virtual_pnl = float(kv.get("virtual_pnl", 0.0))
            STATE.kill = bool(kv.get("kill", False))
            STATE.kill_reason = kv.get("kill_reason")
            STATE.strategy_halted = kv.get("strategy_halted")
            self.failures = int(kv.get("failures", 0))
            self.day = kv.get("day")
            self._seen_deals = set(kv.get("seen_deals", []))
        for o in DB_.load_orders(50):
            STATE.orders.append(o)
            self.orders[o["id"]] = o
        if kv:
            STATE.log("INFO", f"從 DB 還原：部位 {STATE.position} @ {STATE.position_price}，今日已實現 {STATE.realized_pnl}")

    def persist(self):
        with STATE.lock, self.lock:
            DB_.set_kv("broker", {
                "position": STATE.position, "position_price": STATE.position_price,
                "realized_pnl": STATE.realized_pnl, "virtual_pnl": STATE.virtual_pnl,
                "kill": STATE.kill, "kill_reason": STATE.kill_reason,
                "strategy_halted": STATE.strategy_halted,
                "failures": self.failures, "day": self.day,
                "seen_deals": list(self._seen_deals)[-200:],
            })

    def attach(self, api, order_contract, legacy):
        """登入後由 engine 呼叫。"""
        self.api = api
        self.contract = order_contract
        self.legacy = legacy
        with STATE.lock:
            STATE.order_contract = getattr(order_contract, "code", None) if order_contract else None
        if MODE == "signal":
            return
        try:
            self.account = api.futopt_account
        except Exception:
            accts = [a for a in api.list_accounts() if "Future" in type(a).__name__ or
                     str(getattr(a, "account_type", "")).lower().startswith("f")]
            self.account = accts[0] if accts else None
        if self.account is None:
            self._halt("找不到期貨帳號，無法下單")
            return
        if not getattr(self.account, "signed", False):
            STATE.log("WARN", "期貨帳號 signed=False：尚未通過 API 下單簽署，送單會被拒")
        try:
            if legacy:
                api.set_order_callback(self.on_order_event)
            else:
                api.set_order_callback(self.on_order_event)
        except Exception as e:
            STATE.log("WARN", f"註冊委託回報失敗：{e!r}")
        STATE.log("INFO", f"下單層就緒：{MODE} 模式，合約 {STATE.order_contract}，帳號 {self.account.account_id}")

    # ------------------------------------------------------------ 策略入口
    def set_target(self, sign, price, reason):
        """sign: +1 多 / -1 空 / 0 空手。由策略在 K 棒收盤呼叫。"""
        self._roll_day()
        target = max(-MAX_POSITION, min(MAX_POSITION, sign * LOTS))
        with STATE.lock:
            pos = STATE.position
            if STATE.kill:
                STATE.log("WARN", f"kill switch 開啟（{STATE.kill_reason}），忽略訊號 {reason}")
                return
            if STATE.strategy_halted:
                STATE.log("WARN", f"策略已停（{STATE.strategy_halted}），忽略訊號 {reason}")
                return
        delta = target - pos
        if delta == 0:
            return
        side = "多" if target > 0 else "空" if target < 0 else "平"
        STATE.add_signal(side, price, reason)
        notify(f"訊號 {side} @ {price}  {reason}")

        if MODE == "signal":
            self._virtual_fill(target, price)
            return
        if self.pending:
            STATE.log("WARN", f"前一張委託 {self.pending} 未完成，先不送新單")
            return
        self._place(delta, price, reason)

    # ------------------------------------------------------------ 虛擬成交
    def _virtual_fill(self, target, price):
        with STATE.lock:
            if STATE.position != 0 and STATE.position_price is not None:
                STATE.virtual_pnl += (price - STATE.position_price) * STATE.position
            STATE.position = target
            STATE.position_price = price if target else None
        STATE.log("SIGNAL", f"虛擬成交 → 部位 {target} @ {price}")
        self.persist()

    # ------------------------------------------------------------ 送單
    def _place(self, delta, ref_price, reason, retry=0):
        if not in_session(now()):
            STATE.log("WARN", f"非交易時段，不送單（{reason}）")
            return
        if self.api is None or self.contract is None or self.account is None:
            self._halt("下單層未就緒")
            return
        action = Action.Buy if delta > 0 else Action.Sell
        qty = abs(delta)
        oid = f"local-{uuid.uuid4().hex[:8]}"
        rec = {"id": oid, "t": _ts(), "code": STATE.order_contract, "action": "Buy" if delta > 0 else "Sell",
               "qty": qty, "price": 0, "status": "sending", "filled_qty": 0, "avg_price": None,
               "msg": "", "reason": reason, "mode": MODE, "sent_at": time.time(), "retry": retry}
        with self.lock:
            self.orders[oid] = rec
            self.pending = oid
        with STATE.lock:
            STATE.orders.appendleft(rec)
            STATE.pending_order = oid
        try:
            order = FuturesOrder(action=action, price=0, quantity=qty, price_type=FPT.MKT,
                                 order_type=OT.IOC, octype=OC.Auto, account=self.account)
            trade = self.api.place_order(self.contract, order)
            real_id = getattr(trade.order, "id", None) or getattr(trade.order, "seqno", None) or oid
            st = getattr(trade.status, "status", None)
            with self.lock:
                rec["broker_id"] = real_id
                if rec["status"] == "sending":
                    rec["status"] = "sent"
                rec["msg"] = rec.get("msg") or str(getattr(trade.status, "msg", "") or "")
                self.orders[real_id] = rec
            STATE.log("ORDER", f"送單 {rec['action']} {qty} 口 市價 IOC（{reason}）→ {real_id} {st}")
            self._sync_trade(trade)
        except Exception as e:
            self._order_failed(rec, f"送單例外：{e!r}")
        DB_.upsert_order(rec)

    # ------------------------------------------------------------ 回報
    def on_order_event(self, state, event):
        try:
            name = getattr(state, "name", str(state))
            if "Deal" in name:
                self._on_deal(event)
            elif "Order" in name:
                self._on_order_msg(event)
        except Exception as e:
            STATE.log("ERROR", f"委託回報處理錯誤：{e!r} {event}")

    def _get(self, ev, *keys, default=None):
        cur = ev
        for k in keys:
            try:
                cur = cur[k] if not hasattr(cur, "get") else cur.get(k, default)
            except Exception:
                return default
            if cur is None:
                return default
        return cur

    def _on_order_msg(self, ev):
        oid = self._get(ev, "order", "id") or self._get(ev, "order", "seqno")
        op_code = str(self._get(ev, "operation", "op_code", default=""))
        op_msg = str(self._get(ev, "operation", "op_msg", default=""))
        op_type = str(self._get(ev, "operation", "op_type", default=""))
        st = str(self._get(ev, "status", "status", default=""))
        with self.lock:
            rec = self._find_order(oid)
        if rec is None:
            STATE.log("EVENT", f"委託回報（非本程式）{oid} {op_type} {op_code} {op_msg}")
            return
        rec["msg"] = op_msg or rec.get("msg", "")
        if op_code not in ("", "00", "0"):
            self._order_failed(rec, f"委託被拒 {op_code} {op_msg}")
        elif "Cancel" in st or "Cancel" in op_type:
            self._order_cancelled(rec, op_msg)
        else:
            STATE.log("EVENT", f"委託 {oid} {op_type} {st} {op_msg}")
        DB_.upsert_order(rec)

    def _on_deal(self, ev):
        seq = str(self._get(ev, "exchange_seq") or self._get(ev, "seqno") or "")
        oid = self._get(ev, "trade_id") or self._get(ev, "seqno")
        price = float(self._get(ev, "price", default=0) or 0)
        qty = int(self._get(ev, "quantity", default=0) or 0)
        action = str(self._get(ev, "action", default=""))
        code = str(self._get(ev, "code", default=""))
        self._apply_fill(oid, seq, action, price, qty, code)

    def _find_order(self, oid, action=""):
        """回報可能比 place_order 回傳先到，找不到 id 就對到目前 pending 的那張。"""
        rec = self.orders.get(oid)
        if rec is None and self.pending:
            cand = self.orders.get(self.pending)
            if cand and (not action or cand["action"] in action):
                rec = cand
                if oid:
                    self.orders[oid] = cand
                    cand.setdefault("broker_id", oid)
        return rec

    def _apply_fill(self, oid, seq, action, price, qty, code):
        key = f"{oid}:{seq}:{price}:{qty}"
        with self.lock:
            if key in self._seen_deals or qty <= 0:
                return
            self._seen_deals.add(key)
            rec = self._find_order(oid, action)
        signed = qty if "Buy" in action else -qty
        with STATE.lock:
            pos, avg = STATE.position, STATE.position_price or price
            if pos == 0 or (pos > 0) == (signed > 0):
                new_pos = pos + signed
                STATE.position_price = (avg * abs(pos) + price * qty) / abs(new_pos) if new_pos else None
            else:
                closed = min(abs(pos), qty)
                STATE.realized_pnl += (price - avg) * closed * (1 if pos > 0 else -1)
                new_pos = pos + signed
                if new_pos != 0 and (new_pos > 0) != (pos > 0):
                    STATE.position_price = price       # 反手：剩下的是新方向
                elif new_pos == 0:
                    STATE.position_price = None
            STATE.position = new_pos
        fill = {"t": _ts(), "order_id": oid, "code": code, "action": action, "price": price, "qty": qty}
        DB_.add_fill(fill)
        if rec is not None:
            rec["filled_qty"] = rec.get("filled_qty", 0) + qty
            rec["avg_price"] = price if not rec.get("avg_price") else (rec["avg_price"] + price) / 2
            if rec["filled_qty"] >= rec["qty"]:
                rec["status"] = "filled"
                self._clear_pending(rec)
                self.failures = 0
            DB_.upsert_order(rec)
        STATE.log("FILL", f"成交 {action} {qty} @ {price} → 部位 {STATE.position} @ {STATE.position_price}")
        notify(f"成交 {action} {qty} @ {price}\n部位 {STATE.position} @ {STATE.position_price}  今日已實現 {STATE.realized_pnl:.0f} 點")
        self.persist()

    def _sync_trade(self, trade):
        """用查回來的 Trade 物件補回報（回呼漏掉時用）。"""
        try:
            oid = getattr(trade.order, "id", None) or getattr(trade.order, "seqno", None)
            st = str(getattr(trade.status, "status", ""))
            rec = self.orders.get(oid)
            for d in getattr(trade.status, "deals", None) or []:
                self._apply_fill(oid, str(getattr(d, "seq", "")), rec["action"] if rec else "",
                                 float(d.price), int(d.quantity), STATE.order_contract or "")
            if rec and rec["status"] in ("sent", "sending"):
                if "Filled" in st and "Part" not in st:
                    rec["status"] = "filled"; self._clear_pending(rec)
                elif "Cancel" in st:
                    self._order_cancelled(rec, str(getattr(trade.status, "msg", "")))
                elif "Fail" in st or "Reject" in st:
                    self._order_failed(rec, str(getattr(trade.status, "msg", "")))
                DB_.upsert_order(rec)
        except Exception as e:
            STATE.log("WARN", f"同步委託狀態失敗：{e!r}")

    def _clear_pending(self, rec):
        with self.lock:
            if self.pending in (rec["id"], rec.get("broker_id")):
                self.pending = None
        with STATE.lock:
            STATE.pending_order = None

    def _order_cancelled(self, rec, msg):
        if rec["status"] in ("cancelled", "failed", "filled"):
            return
        rec["status"] = "cancelled"
        self._clear_pending(rec)
        STATE.log("WARN", f"委託 {rec['id']} 取消/未成交：{msg}")
        remaining = rec["qty"] - rec.get("filled_qty", 0)
        if remaining > 0 and rec.get("retry", 0) < 1:
            STATE.log("INFO", "重送一次")
            delta = remaining if rec["action"] == "Buy" else -remaining
            self._place(delta, rec.get("price"), rec["reason"] + "（重送）", retry=1)
        else:
            self.failures += 1
            self._check_failures()

    def _order_failed(self, rec, msg):
        if rec["status"] in ("failed", "filled"):
            return
        rec["status"] = "failed"
        rec["msg"] = msg
        self._clear_pending(rec)
        self.failures += 1
        STATE.log("ERROR", f"委託失敗 {rec['id']}：{msg}")
        notify(f"⚠️ 委託失敗：{msg}")
        DB_.upsert_order(rec)
        self._check_failures()

    def _check_failures(self):
        if self.failures >= MAX_ORDER_FAILURES:
            self._halt(f"連續 {self.failures} 次委託失敗")
        self.persist()

    def _halt(self, reason):
        with STATE.lock:
            STATE.strategy_halted = reason
        STATE.log("ERROR", f"策略停止：{reason}（儀表板按「恢復」才會繼續）")
        notify(f"🛑 策略停止：{reason}")
        self.persist()

    # ------------------------------------------------------------ 風控 / 看門狗
    def on_tick(self, price):
        """每筆 tick 呼叫，內部節流到每秒一次。"""
        t = time.time()
        if t - self._last_risk_check < 1:
            return
        self._last_risk_check = t
        self._roll_day()
        with STATE.lock:
            pos, avg = STATE.position, STATE.position_price
            realized, kill = STATE.realized_pnl, STATE.kill
        if pos and avg is not None and DAILY_LOSS_LIMIT > 0 and not kill:
            unreal = (price - avg) * pos
            if realized / max(1, LOTS) + unreal / max(1, abs(pos)) <= -DAILY_LOSS_LIMIT:
                self.kill(f"單日虧損達 {DAILY_LOSS_LIMIT} 點")
                self.flatten("單日虧損上限")
        if FLAT_AT_DAY_CLOSE and pos:
            n = now()
            if n.hour == 13 and n.minute == 44 and n.second >= 20:
                self.flatten("日盤收盤前平倉")

    def tick_watchdog(self):
        """engine 看門狗每 30 秒呼叫。"""
        if MODE == "signal" or self.api is None:
            return
        # 逾時委託
        with self.lock:
            oid = self.pending
        if oid:
            rec = self.orders.get(oid)
            if rec and time.time() - rec.get("sent_at", 0) > ORDER_TIMEOUT:
                STATE.log("WARN", f"委託 {oid} 逾時 {ORDER_TIMEOUT} 秒未回報，主動查詢")
                self._refresh_orders()
        # 對帳
        if time.time() - self._last_reconcile > 60:
            self._last_reconcile = time.time()
            self.reconcile()

    def _refresh_orders(self):
        try:
            self.api.update_status(self.account)
            for tr in self.api.list_trades():
                oid = getattr(tr.order, "id", None) or getattr(tr.order, "seqno", None)
                if oid in self.orders:
                    self._sync_trade(tr)
        except Exception as e:
            STATE.log("WARN", f"查詢委託失敗：{e!r}")

    def reconcile(self):
        try:
            positions = self.api.list_positions(self.account)
            code = STATE.order_contract or ""
            net = 0
            for p in positions:
                if str(getattr(p, "code", "")) != code:
                    continue
                q = int(getattr(p, "quantity", 0))
                net += q if "Buy" in str(getattr(p, "direction", "")) else -q
            with STATE.lock:
                mine = STATE.position
                STATE.broker_position = net
                STATE.reconcile_ok = (net == mine)
            if net != mine:
                STATE.log("ERROR", f"部位不一致：內部 {mine}，券商 {net}" + ("，改以券商為準" if RECONCILE_ADOPT else ""))
                notify(f"⚠️ 部位不一致：內部 {mine} / 券商 {net}", key="reconcile", cooldown=300)
                if RECONCILE_ADOPT:
                    with STATE.lock:
                        STATE.position = net
                        if net == 0:
                            STATE.position_price = None
                    self.persist()
        except Exception as e:
            STATE.log("WARN", f"對帳失敗：{e!r}")

    def _roll_day(self):
        """跨交易日（夜盤 05:00 收後）重置今日損益與 kill（僅虧損上限造成的）。"""
        n = now()
        if n.hour < 5:
            day = (n.date().toordinal() - 1)
        else:
            day = n.date().toordinal()
        if self.day is None:
            self.day = day
            return
        if day != self.day:
            self.day = day
            with STATE.lock:
                STATE.realized_pnl = 0.0
                if STATE.kill and STATE.kill_reason and "單日虧損" in STATE.kill_reason:
                    STATE.kill, STATE.kill_reason = False, None
            STATE.log("INFO", "新交易日，今日損益歸零")
            self.persist()

    # ------------------------------------------------------------ 手動操作
    def kill(self, reason):
        with STATE.lock:
            STATE.kill, STATE.kill_reason = True, reason
        STATE.log("WARN", f"kill switch 開啟：{reason}")
        notify(f"🛑 kill switch：{reason}")
        self.persist()

    def resume(self):
        with STATE.lock:
            STATE.kill, STATE.kill_reason = False, None
            STATE.strategy_halted = None
        self.failures = 0
        STATE.log("INFO", "已恢復（kill 關閉、策略停止解除、失敗計數歸零）")
        self.persist()

    def flatten(self, reason="手動平倉"):
        with STATE.lock:
            pos = STATE.position
            price = STATE.last_price
        if pos == 0:
            STATE.log("INFO", f"{reason}：目前無部位")
            return
        STATE.log("WARN", f"{reason}：平掉 {pos} 口")
        if MODE == "signal":
            self._virtual_fill(0, price or 0)
        else:
            with self.lock:
                self.pending = None   # 平倉優先，蓋掉卡住的 pending
            self._place(-pos, price, reason)


BROKER = Broker()
