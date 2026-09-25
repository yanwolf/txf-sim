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
from .state import STATE, in_session, now, session_dead

MODE = os.getenv("MODE", "signal").lower()
SIMULATION = os.getenv("SIMULATION", "true").lower() != "false"
ENV_TAG = "sim" if SIMULATION else "live"
KV_KEY = f"broker:{ENV_TAG}"      # 正式與模擬的部位/損益/kill 分開存，切換環境不會互相污染
LOTS = int(os.getenv("LOTS", "1"))
MAX_POSITION = int(os.getenv("MAX_POSITION", "4"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT_PTS", "300"))   # 每口點數，0 = 關閉
ORDER_TIMEOUT = int(os.getenv("ORDER_TIMEOUT", "15"))
MAX_ORDER_FAILURES = int(os.getenv("MAX_ORDER_FAILURES", "2"))
FLAT_AT_DAY_CLOSE = os.getenv("FLAT_AT_DAY_CLOSE", "false").lower() == "true"
RECONCILE_ADOPT = os.getenv("RECONCILE_ADOPT", "false").lower() == "true"
MARGIN_ALERT_AVAILABLE = float(os.getenv("MARGIN_ALERT_AVAILABLE", "0"))   # 可用保證金低於此金額（元）就通知；0 = 不檢查
SPLIT_FLIP = os.getenv("SPLIT_FLIP", "true").lower() == "true"   # 反手拆成「平倉」+「新倉」兩張單

# 新舊版 shioaji 常數相容
_C = sj.constant
Action = getattr(sj, "Action", None) or _C.Action
FPT = getattr(sj, "FuturesPriceType", None) or _C.FuturesPriceType
OT = getattr(sj, "OrderType", None) or getattr(_C, "OrderType", None) or _C.FuturesOrderType
OC = getattr(sj, "FuturesOCType", None) or _C.FuturesOCType
FuturesOrder = getattr(sj, "FuturesOrder", None) or sj.Order


def _ts():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def _net_from_positions(positions, order_code):
    """把券商部位加總成淨口數。以商品前綴（例如 TMF）比對，避免月份代碼格式差異導致讀成 0。"""
    root = (order_code or "")[:3]
    net, rows, avg, by_code = 0, [], None, {}
    for p in positions or []:
        pc = str(getattr(p, "code", ""))
        q = abs(int(getattr(p, "quantity", 0) or 0))
        d = str(getattr(p, "direction", ""))
        rows.append(f"{pc} {d} {q}")
        if not root or not pc.startswith(root):
            continue
        signed = q if "Buy" in d else -q
        net += signed
        by_code[pc] = by_code.get(pc, 0) + signed
        if getattr(p, "price", None):
            avg = float(p.price)
    return net, rows, avg, by_code


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
        self._mismatch_streak = 0
        self._deferred = None      # pending 時來的新目標，成交後補送
        self.ready = False         # 下單層就緒且已完成啟動對帳
        self._last_fill_ts = 0.0
        self.contract_lookup = None   # engine 注入：代碼 → 合約物件
        self._roll_target = None
        self._roll_wait_logged = None
        self._startup_fail = 0
        self._last_startup_try = 0.0
        self._recon_fail = 0
        self.margin = None              # 最近一次保證金查詢結果（dict）
        self._last_margin = 0.0
        STATE.mode = MODE

    # ------------------------------------------------------------ 初始化
    def restore(self):
        """從 DB 讀回部位與風控狀態（容器重啟用）。"""
        kv = DB_.get_kv(KV_KEY)
        if kv is None and ENV_TAG == "sim":
            kv = DB_.get_kv("broker")          # 舊版單一 key 只可能是模擬環境的資料
        kv = kv or {}
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
            if o.get("status") in ("sent", "sending"):
                # 重啟前沒等到終態的委託：標成未知，避免被當成進行中
                o["status"] = "unknown"
                o["msg"] = (o.get("msg") or "") + " 重啟前未回報"
                DB_.upsert_order(o)
            STATE.orders.append(o)
            self.orders[o["id"]] = o
            if o.get("broker_id"):
                self.orders[o["broker_id"]] = o
        if kv:
            STATE.log("INFO", f"從 DB 還原（{ENV_TAG}）：部位 {STATE.position} @ {STATE.position_price}，今日已實現 {STATE.realized_pnl}")
        else:
            STATE.log("INFO", f"{ENV_TAG} 環境無既有狀態，從零開始（部位以券商查詢為準）")

    def persist(self):
        with STATE.lock, self.lock:
            DB_.set_kv(KV_KEY, {
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
            self.ready = True
            return
        self.ready = False
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
        if not self._startup_reconcile():
            return
        STATE.log("INFO", f"下單層就緒：{MODE}（{'模擬' if SIMULATION else '正式'}）模式，合約 {STATE.order_contract}，帳號 {self.account.account_id}")

    # ------------------------------------------------------------ 策略入口
    def set_target(self, sign, price, reason):
        """單策略介面（保留相容）：sign +1/-1/0 × LOTS。"""
        self.set_net(sign * LOTS, price, reason)

    def set_net(self, net, price, reason):
        """多策略介面：把券商部位調整成 net 口（正多負空）。"""
        self._roll_day()
        target = max(-MAX_POSITION, min(MAX_POSITION, int(net)))
        with STATE.lock:
            pos = STATE.position
            if STATE.kill:
                STATE.log("WARN", f"kill switch 開啟（{STATE.kill_reason}），忽略 {reason}（目標 {target}）")
                return
            if STATE.strategy_halted:
                STATE.log("WARN", f"策略已停（{STATE.strategy_halted}），忽略 {reason}（目標 {target}）")
                return
        delta = target - pos
        if delta == 0:
            return
        if MODE == "signal":
            self._virtual_fill(target, price)
            return
        if self.pending:
            STATE.log("WARN", f"前一張委託 {self.pending} 未完成，先不送新單（目標 {target}）")
            self._deferred = (target, price, reason)
            return
        if SPLIT_FLIP and pos != 0 and target != 0 and (pos > 0) != (target > 0):
            self._place(-pos, price, reason + "／平倉", octype="Cover",
                        then=(target, price, reason + "／新倉"))
        else:
            oc = "Cover" if (pos != 0 and abs(target) < abs(pos) and (target == 0 or (target > 0) == (pos > 0))) else "Auto"
            self._place(delta, price, reason, octype=oc)

    def _startup_reconcile(self):
        """開機對帳：券商是唯一真相，開機時一律採用券商部位（不受 RECONCILE_ADOPT 影響）。
        失敗回 False，看門狗每分鐘重試；成功前 ready=False，不會送任何單。"""
        self._last_startup_try = time.time()
        try:
            positions = self.api.list_positions(self.account)
            net, rows, avg, by_code = _net_from_positions(positions, STATE.order_contract)
            STATE.log("INFO", f"啟動對帳：券商原始部位 {rows or '空'}")
            with STATE.lock:
                old = STATE.position
                STATE.position = net
                STATE.broker_position = net
                STATE.reconcile_ok = True
                if net == 0:
                    STATE.position_price = None
                elif old != net or STATE.position_price is None:
                    STATE.position_price = avg or STATE.last_price
            if old != net:
                STATE.log("WARN", f"啟動對帳：內部 {old} → 採用券商實際部位 {net}")
            else:
                STATE.log("INFO", f"啟動對帳：券商部位 {net}，與內部一致")
            held_other = {c: q for c, q in by_code.items()
                          if q and c != STATE.order_contract and c[3:4] in "ABCDEFGHIJKL"}
            if held_other and self.contract_lookup:
                oc = list(held_other)[0]
                oldc = self.contract_lookup(oc)
                if oldc is not None:
                    self._roll_target = self.contract          # 換月的目標（應下的月份）
                    self.contract = oldc                       # 先指向實際持有的舊月，由 roll_to 接手
                    with STATE.lock:
                        STATE.order_contract = oldc.code
                    STATE.log("WARN", f"啟動對帳：帳戶持有 {oc} {held_other[oc]} 口（非目前下單月份），將執行換月")
            self.persist()
            self.ready = True
        except Exception as e:
            self.ready = False
            self._startup_fail += 1
            if session_dead(e):
                STATE.need_relogin = STATE.need_relogin or "開機對帳回報憑證過期／連線未建立"
            if self._startup_fail == 1:
                STATE.log("WARN", f"啟動對帳暫時失敗，每分鐘重試，成功前不下單：{e!r}")
                if in_session(now()):
                    notify(f"⚠️ 啟動對帳失敗，重試中（成功前不下單）：{e!r}")
            elif self._startup_fail % 30 == 0:
                STATE.log("WARN", f"啟動對帳仍失敗（已重試 {self._startup_fail} 次）：{e!r}")
            return False
        if self._startup_fail:
            STATE.log("INFO", f"啟動對帳成功（重試 {self._startup_fail} 次後）")
            notify("啟動對帳成功，下單層恢復")
        self._startup_fail = 0
        self.refresh_margin()
        return True

    def refresh_margin(self):
        """查詢期貨帳戶保證金。盤後券商查詢服務可能停擺，失敗就保留上一次的結果。"""
        if self.api is None or self.account is None:
            return
        self._last_margin = time.time()
        try:
            m = self.api.margin(self.account)
            g = lambda k: float(getattr(m, k, 0) or 0)
            self.margin = {
                "equity": g("equity"), "equity_amount": g("equity_amount"),
                "available_margin": g("available_margin"),
                "initial_margin": g("initial_margin"), "maintenance_margin": g("maintenance_margin"),
                "margin_call": g("margin_call"), "risk_indicator": g("risk_indicator"),
                "today_balance": g("today_balance"), "future_open_position": g("future_open_position"),
                "future_settle_profitloss": g("future_settle_profitloss"),
                "t": now().strftime("%m-%d %H:%M"),
            }
            if self.margin["margin_call"] > 0:
                STATE.log("ERROR", f"保證金追繳：{self.margin['margin_call']:,.0f} 元")
                notify(f"🛑 保證金追繳 {self.margin['margin_call']:,.0f} 元，請盡快處理", key="margin_call", cooldown=1800)
            if MARGIN_ALERT_AVAILABLE > 0 and self.margin["available_margin"] < MARGIN_ALERT_AVAILABLE:
                STATE.log("WARN", f"可用保證金 {self.margin['available_margin']:,.0f} 元，低於警示 {MARGIN_ALERT_AVAILABLE:,.0f}")
                notify(f"⚠️ 可用保證金 {self.margin['available_margin']:,.0f} 元，低於警示門檻 {MARGIN_ALERT_AVAILABLE:,.0f}",
                       key="margin_low", cooldown=3600)
        except Exception as e:
            if session_dead(e):
                STATE.need_relogin = STATE.need_relogin or "保證金查詢回報憑證過期／連線未建立"
            if self.margin is None or in_session(now()):
                STATE.log("WARN", f"保證金查詢失敗：{e!r}")

    def roll_to(self, new_contract):
        """把下單合約換到 new_contract；手上若有舊月部位，先平舊月、成交後開同向同量的新月。"""
        old = self.contract
        new_code = getattr(new_contract, "code", None)
        if new_code is None or (old is not None and old.code == new_code):
            return
        with STATE.lock:
            pos, price = STATE.position, STATE.last_price
            kill, halted = STATE.kill, STATE.strategy_halted
        tag = f"{getattr(old, 'code', '—')} → {new_code}"

        def _switch():
            self.contract = new_contract
            with STATE.lock:
                STATE.order_contract = new_code
            self.persist()

        if MODE == "signal" or pos == 0:
            _switch()
            STATE.log("INFO", f"下單換月：{tag}（空手，直接切換）")
            notify(f"下單換月：{tag}（空手）")
            return
        if not self.ready or self.pending:
            return
        if not in_session(now()) or STATE.session_note or price is None:
            if self._roll_wait_logged != new_code:
                self._roll_wait_logged = new_code
                STATE.log("WARN", f"換月待執行：{tag}，持倉 {pos} 口，等開盤有報價再換")
            return
        if kill or halted:
            # 風控停止中：只平舊月，不開新月
            STATE.log("WARN", f"換月（風控停止中，只平舊月）：{tag}")
            _switch()
            self._place(-pos, price, f"換月 {tag}／只平舊月", octype="Cover", contract=old, roll=True)
            return
        STATE.log("WARN", f"換月：{tag}，持倉 {pos} 口，先平舊月再開新月")
        notify(f"換月：{tag}，持倉 {pos} 口")
        _switch()   # 先切換；兩張單都明確指定月份，不依賴切換與回報的先後
        self._place(-pos, price, f"換月 {tag}／平舊月", octype="Cover", contract=old, roll=True,
                    then=(pos, price, f"換月 {tag}／開新月", new_contract))

    # ------------------------------------------------------------ 虛擬成交
    def _virtual_fill(self, target, price):
        with STATE.lock:
            pos, avg = STATE.position, STATE.position_price
            if pos != 0 and avg is not None and (target == 0 or (target > 0) != (pos > 0) or abs(target) < abs(pos)):
                closed = abs(pos) if (target == 0 or (target > 0) != (pos > 0)) else abs(pos) - abs(target)
                STATE.virtual_pnl += (price - avg) * closed * (1 if pos > 0 else -1)
            if target == 0:
                STATE.position_price = None
            elif pos == 0 or (target > 0) != (pos > 0):
                STATE.position_price = price
            elif abs(target) > abs(pos):
                STATE.position_price = (avg * abs(pos) + price * (abs(target) - abs(pos))) / abs(target)
            STATE.position = target
        STATE.log("SIGNAL", f"虛擬成交 → 部位 {target} @ {price}")
        self.persist()

    # ------------------------------------------------------------ 送單
    def _place(self, delta, ref_price, reason, retry=0, octype="Auto", then=None, contract=None, roll=False):
        if not in_session(now()) or STATE.session_note:
            STATE.log("WARN", f"非交易時段或推定休市，不送單（{reason}）")
            return
        contract = contract or self.contract
        if self.api is None or contract is None or self.account is None or not self.ready:
            STATE.log("WARN", f"下單層尚未就緒，略過（{reason}）；就緒後會自動對齊")
            return
        action = Action.Buy if delta > 0 else Action.Sell
        qty = abs(delta)
        oid = f"local-{uuid.uuid4().hex[:8]}"
        rec = {"id": oid, "t": _ts(), "code": getattr(contract, "code", STATE.order_contract), "action": "Buy" if delta > 0 else "Sell",
               "qty": qty, "price": 0, "status": "sending", "filled_qty": 0, "avg_price": None,
               "msg": "", "reason": reason, "mode": MODE, "sent_at": time.time(), "retry": retry,
               "octype": octype, "then": then, "_contract": contract, "roll": roll}
        with self.lock:
            self.orders[oid] = rec
            self.pending = oid
        with STATE.lock:
            STATE.orders.appendleft(rec)
            STATE.pending_order = oid
        try:
            oc = {"Cover": OC.Cover, "New": OC.New}.get(octype, OC.Auto)
            order = FuturesOrder(action=action, price=0, quantity=qty, price_type=FPT.MKT,
                                 order_type=OT.IOC, octype=oc, account=self.account)
            trade = self.api.place_order(contract, order)
            real_id = getattr(trade.order, "id", None) or getattr(trade.order, "seqno", None) or oid
            st = getattr(trade.status, "status", None)
            with self.lock:
                rec["broker_id"] = real_id
                if rec["status"] == "sending":
                    rec["status"] = "sent"
                rec["msg"] = rec.get("msg") or str(getattr(trade.status, "msg", "") or "")
                self.orders[real_id] = rec
            STATE.log("ORDER", f"送單 {rec['action']} {qty} 口 市價 IOC {octype}（{reason}）→ {real_id} {st}")
            self._sync_trade(trade)
        except Exception as e:
            if session_dead(e):
                STATE.need_relogin = STATE.need_relogin or "送單回報憑證過期／連線未建立"
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
        self._last_fill_ts = time.time()
        fill = {"t": _ts(), "order_id": oid, "code": code, "action": action, "price": price, "qty": qty}
        DB_.add_fill(fill)
        if rec is not None:
            rec["filled_qty"] = rec.get("filled_qty", 0) + qty
            rec["avg_price"] = price if not rec.get("avg_price") else (rec["avg_price"] + price) / 2
            nxt = None
            if rec["filled_qty"] >= rec["qty"]:
                rec["status"] = "filled"
                self._clear_pending(rec)
                self.failures = 0
                nxt = rec.pop("then", None)
            DB_.upsert_order(rec)
            if nxt:
                target, p2, r2 = nxt[:3]
                c2 = nxt[3] if len(nxt) > 3 else rec.get("_contract")   # 未指定就沿用同一張單的月份
                threading.Thread(target=self._place, args=(target, p2, r2),
                                 kwargs={"octype": "New", "contract": c2}, daemon=True).start()
            elif rec["status"] == "filled" and self._deferred and not self.pending:
                d, self._deferred = self._deferred, None
                threading.Thread(target=self.set_net, args=d, daemon=True).start()
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
            self._place(delta, rec.get("price"), rec["reason"] + "（重送）", retry=1,
                        octype=rec.get("octype", "Auto"), then=rec.get("then"),
                        contract=rec.get("_contract"), roll=rec.get("roll", False))
        else:
            self.failures += 1
            if rec.get("roll"):
                rec["then"] = None
                self._halt(f"換月平舊月未成交（{rec.get('code')}），請手動確認舊月部位")
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
        if rec.get("roll"):
            rec["then"] = None
            self._halt(f"換月平舊月失敗（{rec.get('code')}），請手動確認舊月部位")
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
            age = time.time() - rec.get("sent_at", 0) if rec else 0
            if rec and age > ORDER_TIMEOUT * 4:
                # 卡超過 60 秒：主動刪單、記失敗、放行後面的訊號
                STATE.log("ERROR", f"委託 {oid} 卡住 {int(age)} 秒，主動刪單並標記失敗")
                self._cancel_stuck(rec)
            elif rec and age > ORDER_TIMEOUT:
                STATE.log("WARN", f"委託 {oid} 逾時 {int(age)} 秒未回報，主動查詢")
                self._refresh_orders(oid)
        # 開機對帳失敗 → 每分鐘重試
        if not self.ready and self.account is not None:
            if time.time() - self._last_startup_try > 60:
                if self._startup_reconcile():
                    STATE.log("INFO", f"下單層就緒：{MODE}（{'模擬' if SIMULATION else '正式'}）模式，合約 {STATE.order_contract}")
            return
        # 保證金：盤中每 5 分鐘
        if time.time() - self._last_margin > 300 and in_session(now()) and not STATE.session_note:
            self.refresh_margin()
        # 例行對帳：只在盤中（盤後券商查詢服務會回錯誤，且此時不會下單）
        if time.time() - self._last_reconcile > 60 and in_session(now()) and not STATE.session_note:
            self._last_reconcile = time.time()
            self.reconcile()

    def _find_trade(self, rec):
        ids = {rec.get("id"), rec.get("broker_id")}
        for tr in self.api.list_trades():
            tid = getattr(tr.order, "id", None) or getattr(tr.order, "seqno", None)
            if tid in ids:
                return tr
        return None

    def _refresh_orders(self, oid=None):
        try:
            self.api.update_status(self.account)
            rec = self.orders.get(oid) if oid else None
            if rec:
                tr = self._find_trade(rec)
                if tr is None:
                    STATE.log("WARN", f"券商委託清單查無 {rec.get('broker_id') or oid}")
                    return
                st = getattr(tr.status, "status", "")
                deals = getattr(tr.status, "deals", None) or []
                STATE.log("INFO", f"券商回報 {rec.get('broker_id')}：{st} 成交 {len(deals)} 筆 {getattr(tr.status, 'msg', '')}")
                self._sync_trade(tr)
                return
            for tr in self.api.list_trades():
                tid = getattr(tr.order, "id", None) or getattr(tr.order, "seqno", None)
                if tid in self.orders:
                    self._sync_trade(tr)
        except Exception as e:
            STATE.log("WARN", f"查詢委託失敗：{e!r}")

    def _cancel_stuck(self, rec):
        try:
            self.api.update_status(self.account)
            tr = self._find_trade(rec)
            if tr is not None:
                self._sync_trade(tr)              # 可能其實已成交，先同步一次
                if rec["status"] in ("filled", "cancelled", "failed"):
                    return
                try:
                    self.api.cancel_order(tr)
                    STATE.log("WARN", f"已送出刪單 {rec.get('broker_id')}")
                except Exception as e:
                    STATE.log("WARN", f"刪單失敗：{e!r}")
        except Exception as e:
            STATE.log("WARN", f"處理卡單失敗：{e!r}")
        if rec["status"] not in ("filled", "cancelled", "failed"):
            self._order_failed(rec, "逾時無回報，已放棄此委託")
            notify("⚠️ 委託卡住已放棄，下一次訊號會重新對齊部位；請確認券商實際部位", key="stuck", cooldown=300)

    def reconcile(self, manual=False):
        try:
            positions = self.api.list_positions(self.account)
            net, rows, _, _ = _net_from_positions(positions, STATE.order_contract)
            with STATE.lock:
                mine = STATE.position
                STATE.broker_position = net
            if net == mine:
                if self._mismatch_streak:
                    STATE.log("INFO", f"部位已重新一致：{net}")
                self._mismatch_streak = 0
                with STATE.lock:
                    STATE.reconcile_ok = True
                    STATE.mismatch_min = 0
                return
            # 不一致：先查自己送過的委託有沒有事後成交（回報漏掉的情況）
            self._refresh_orders()
            with STATE.lock:
                mine = STATE.position
            if net == mine:
                STATE.log("INFO", f"補記漏掉的成交後已一致：{net}")
                self._mismatch_streak = 0
                with STATE.lock:
                    STATE.reconcile_ok = True
                    STATE.mismatch_min = 0
                return
            self._mismatch_streak += 1
            with STATE.lock:
                STATE.reconcile_ok = False
                STATE.mismatch_min = self._mismatch_streak
            if self._mismatch_streak == 1 and not manual:
                STATE.log("WARN", f"部位不一致：內部 {mine}，券商 {net}（觀察中，券商查詢可能延遲）")
                return
            if self._mismatch_streak == 3 or manual or self._mismatch_streak % 10 == 0:
                STATE.log("ERROR", f"部位不一致已持續 {self._mismatch_streak} 分鐘：內部 {mine}，券商 {net}" + ("，改以券商為準" if RECONCILE_ADOPT else ""))
                STATE.log("WARN", f"券商原始部位回傳：{rows or '空'}")
                notify(f"⚠️ 部位不一致 {self._mismatch_streak} 分鐘：內部 {mine} / 券商 {net}", key="reconcile", cooldown=600)
            if RECONCILE_ADOPT and self._mismatch_streak >= 3:
                if time.time() - self._last_fill_ts < 300:
                    STATE.log("WARN", "5 分鐘內剛有成交，券商查詢可能延遲，暫不自動採用券商部位")
                else:
                    self.adopt_broker()
        except Exception as e:
            self._recon_fail += 1
            if session_dead(e):
                STATE.need_relogin = STATE.need_relogin or "對帳回報憑證過期／連線未建立"
            if self._recon_fail == 1 or self._recon_fail % 30 == 0:
                STATE.log("WARN", f"對帳失敗（連續 {self._recon_fail} 次）：{e!r}")
            return
        if self._recon_fail:
            STATE.log("INFO", f"對帳恢復（先前連續失敗 {self._recon_fail} 次）")
            self._recon_fail = 0
        self.margin = None              # 最近一次保證金查詢結果（dict）
        self._last_margin = 0.0

    def adopt_broker(self):
        """手動或自動：內部帳改成券商的數字。"""
        with STATE.lock:
            net = STATE.broker_position
            if net is None:
                return
            old = STATE.position
            STATE.position = net
            if net == 0:
                STATE.position_price = None
            elif STATE.position_price is None:
                STATE.position_price = STATE.last_price
            STATE.reconcile_ok = True
            STATE.mismatch_min = 0
        self._mismatch_streak = 0
        STATE.log("WARN", f"內部部位 {old} → 改為券商的 {net}")
        notify(f"部位已以券商為準：{old} → {net}")
        self.persist()

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
            self._place(-pos, price, reason, octype="Cover")


BROKER = Broker()
