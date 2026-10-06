"""订单提供者：把冻结的操作单/人类计划变成模拟线的日内订单（实施方案 §6A.1–6A.3）。

- `TicketProvider`：按唯一选择规则取「截止前最后一个成功冻结且仍有效」的操作单；`state_ref` 与当前线内状态不符即失效。
- `HumanPlanProvider`：取「截止前且首次揭示前」的人类计划；缺失＝无订单。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal

from mystock2.coach.intents import select_human_plan
from mystock2.coach.tickets import Cell, select_ticket
from mystock2.core import calendars as cal
from mystock2.core.money import dec, floor_to_lots
from mystock2.core.timeutil import ensure_utc
from mystock2.ledger.fees import FeeRule, max_buy_cost, possible_fills_bound, select_rule
from mystock2.scoreboard.types import BUY, SELL, ExecProtocol, LineState, SimOrder


class TicketProvider:
    def __init__(self, conn: sqlite3.Connection, *, batch_id: str, line_id: str, market: str, codes: list[str], kind: str = "line_sim"):
        self.conn, self.batch_id, self.line_id, self.market, self.codes, self.kind = conn, batch_id, line_id, market, codes, kind
        self.last_reasons: dict[tuple[date, str], str] = {}

    def __call__(self, state: LineState, day: date) -> list[SimOrder]:
        deadline = cal.project_deadline(self.market, day)
        orders = []
        for code in self.codes:
            sel = select_ticket(self.conn, Cell(self.batch_id, self.line_id, self.kind, self.market, day.isoformat(), code),
                                deadline_at=deadline, current_state_ref=state.hash())
            self.last_reasons[(day, code)] = sel.reason
            t = sel.ticket
            if t is None or t["action"] not in (BUY, SELL) or not t["qty"] or t["limit_price"] is None:
                continue
            orders.append(SimOrder(self.line_id, code, t["action"], int(t["qty"]), dec(t["limit_price"]), t["ticket_id"],
                                   ensure_utc(t["valid_to"]) if t["valid_to"] else None))
        return orders


class HumanPlanProvider:
    """人类计划 → 订单。传入 `fee_rules` 时按**整组**累计预算（审核 P1-11）：记录时每条计划只对全部可交易现金单独检查，
    组合起来可能超预算；这里按固定顺序（卖单在前，买单按代码字母序）累计，超出部分按协议预注册的 `constraint_handling`
    截断到整手（truncate）或整条作废（reject），并记标记——不留给撮合时整单拒绝。"""

    def __init__(self, conn: sqlite3.Connection, *, batch_id: str, line_id: str, market: str, codes: list[str],
                 fee_rules: list[FeeRule] | None = None, lot_sizes: dict[str, int] | None = None, constraint_handling: str = "reject",
                 protocol: ExecProtocol | None = None):
        self.conn, self.batch_id, self.line_id, self.market, self.codes = conn, batch_id, line_id, market, codes
        self.fee_rules, self.lot_sizes, self.handling, self.protocol = fee_rules, lot_sizes or {}, constraint_handling, protocol or ExecProtocol()
        self.flags: dict[tuple[date, str], list[str]] = {}

    def __call__(self, state: LineState, day: date) -> list[SimOrder]:
        plan = select_human_plan(self.conn, batch_id=self.batch_id, line_id=self.line_id, market=self.market, target_session=day, codes=self.codes,
                                 deadline_at=cal.project_deadline(self.market, day))
        orders = []
        for code, p in plan.items():
            self.flags[(day, code)] = list(p["flags"])
            if p["action"] in (BUY, SELL):
                if p.get("state_hash") and p["state_hash"] != state.hash():       # 计划是针对另一个状态记录的：失效，按无订单（与操作单的 state_ref 规则一致）
                    self.flags[(day, code)].append("state_changed")
                    continue
                orders.append(SimOrder(self.line_id, code, p["action"], int(p["qty"]), Decimal(p["limit_price"]), p["intent_id"],
                                       ensure_utc(p["valid_to"]) if p.get("valid_to") else None))
        if self.fee_rules is None:
            return orders
        return self._group_budget(state, day, orders)

    def _group_budget(self, state: LineState, day: date, orders: list[SimOrder]) -> list[SimOrder]:
        sells = [o for o in orders if o.side == SELL]
        buys = sorted((o for o in orders if o.side == BUY), key=lambda o: o.code)
        rule = select_rule(self.fee_rules, self.market, BUY, day.isoformat())
        bound = possible_fills_bound(self.market, day)
        left = state.tradable_cash()
        out = list(sells)
        for o in buys:
            def need(n: int, o=o) -> Decimal:
                return max_buy_cost(rule, n, o.limit_price, slippage_bps=self.protocol.slippage_bps, fee_multiplier=self.protocol.fee_multiplier,
                                    possible_fills=bound)
            q = o.qty
            if need(q) > left:
                lot = self.lot_sizes.get(o.code, 1)
                if self.handling == "truncate":
                    while q > 0 and need(q) > left:
                        q -= lot
                    q = int(floor_to_lots(Decimal(q), lot)) if q > 0 else 0
                else:
                    q = 0
                self.flags.setdefault((day, o.code), []).append("group_budget_truncated" if q > 0 else "group_budget_exceeded")
                if q <= 0:
                    continue
                o = replace(o, qty=q)
            left -= need(q)
            out.append(o)
        return out
