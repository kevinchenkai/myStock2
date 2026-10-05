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
    """as_of 时点尚未结算的卖出回款（逐币种）。结算日当天视为已结算。"""
    as_of = ensure_utc(as_of)
    out: dict[str, Decimal] = {}
    q = ("SELECT market, currency, cash_delta, event_at FROM ledger_event WHERE account_id=? AND event_type='FILL' AND qty_delta LIKE '-%' "
         "AND event_at <= ?")
    from mystock2.core.timeutil import iso_utc
    for r in conn.execute(q, (account_id, iso_utc(as_of))):
        rule = rules.get(r["market"]) or SettlementRule(r["market"], None)
        trade_day = to_market_time(r["event_at"], r["market"]).date()
        sd = settle_date(rule, trade_day)
        settled_by = to_market_time(as_of, r["market"]).date()
        if settled_by < sd:
            out[r["currency"]] = out.get(r["currency"], Decimal(0)) + dec(r["cash_delta"])
    return out


def tradable_cash(economic_cash: dict[str, Decimal], unsettled: dict[str, Decimal], reserved: dict[str, Decimal] | None = None) -> dict[str, Decimal]:
    res = reserved or {}
    return {c: economic_cash.get(c, Decimal(0)) - unsettled.get(c, Decimal(0)) - res.get(c, Decimal(0)) for c in set(economic_cash) | set(unsettled) | set(res)}
