"""经济现金 vs 可交易现金（实施方案 §6 不变量 9、WP2.6、T-23）。

经济现金＝Σ cash_delta；可交易现金＝经济现金 − 未结算的卖出回款 − 冻结/预留。**结算周期不预设**：
美股 T+1、港股 T+2 等须由 M0a 核实并写入配置，未配置时 `settle_date` 报错而不是默默当作 T+0。
不把融资买力当自有现金：这里的「现金」只来自账本。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc, to_market_time


class SettlementUnknown(LookupError):
    pass


@dataclass(frozen=True)
class SettlementRule:
    market: str
    lag_sessions: int | None      # None＝未核实/未配置


def settle_date(rule: SettlementRule, trade_day: date) -> date:
    if rule.lag_sessions is None:
        raise SettlementUnknown(f"{rule.market} 的结算周期未配置（须按交易所/券商规则核实，M0a）")
    d = trade_day
    for _ in range(rule.lag_sessions):
        d = cal.next_session(rule.market, d)
    return d


def unsettled_sell_proceeds(conn: sqlite3.Connection, account_id: str, rules: dict[str, SettlementRule], as_of: datetime) -> dict[str, Decimal]:
    """as_of 时点尚未结算的卖出回款（逐币种）。结算日当天视为已结算。

    只统计**有效**卖出（更正/取消后的当前有效版本）且在开账点之后的成交——与账本投影共享同一边界与更正语义。
    """
    from mystock2.ledger.projection import effective_events

    as_of = ensure_utc(as_of)
    opening = conn.execute("SELECT opening_at FROM account_opening WHERE account_id=?", (account_id,)).fetchone()
    t0 = opening["opening_at"] if opening else None
    out: dict[str, Decimal] = {}
    for r in effective_events(conn, account_id):
        if r["event_type"] != "FILL" or not r["qty_delta"].startswith("-") or ensure_utc(r["event_at"]) > as_of:
            continue
        if t0 is not None and r["event_at"] <= t0:
            continue
        rule = rules.get(r["market"]) or SettlementRule(r["market"], None)
        sd = settle_date(rule, to_market_time(r["event_at"], r["market"]).date())
        if to_market_time(as_of, r["market"]).date() < sd:
            out[r["currency"]] = out.get(r["currency"], Decimal(0)) + dec(r["cash_delta"])
    return out


def tradable_cash(economic_cash: dict[str, Decimal], unsettled: dict[str, Decimal], reserved: dict[str, Decimal] | None = None) -> dict[str, Decimal]:
    res = reserved or {}
    return {c: economic_cash.get(c, Decimal(0)) - unsettled.get(c, Decimal(0)) - res.get(c, Decimal(0)) for c in set(economic_cash) | set(unsettled) | set(res)}
