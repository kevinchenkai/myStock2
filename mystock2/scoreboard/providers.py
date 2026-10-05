"""订单提供者：把冻结的操作单/人类计划变成模拟线的日内订单（实施方案 §6A.1–6A.3）。

- `TicketProvider`：按唯一选择规则取「截止前最后一个成功冻结且仍有效」的操作单；`state_ref` 与当前线内状态不符即失效。
- `HumanPlanProvider`：取「截止前且首次揭示前」的人类计划；缺失＝无订单。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from mystock2.coach.intents import select_human_plan
from mystock2.coach.tickets import Cell, select_ticket
from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc
from mystock2.scoreboard.types import BUY, SELL, LineState, SimOrder


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
    def __init__(self, conn: sqlite3.Connection, *, batch_id: str, line_id: str, market: str, codes: list[str]):
        self.conn, self.batch_id, self.line_id, self.market, self.codes = conn, batch_id, line_id, market, codes
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
        return orders
