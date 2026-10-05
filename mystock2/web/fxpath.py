"""汇率换算路径：直接/反向币对，或经 USD 中转（USD/HKD/CNY，实施方案 LN-06）。

只读：包装 `mystock2.market.fx.get_rate`。缺任何一段即抛 `FxUnavailable`——调用方显示「不可用」，
**不静默当作 1，也不拿过旧的值冒充**。返回每一段的来源与日期，供页面显示「汇率来源与时间」。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mystock2.market.fx import FxUnavailable, get_rate

PIVOT = "USD"


@dataclass
class FxResolution:
    from_ccy: str
    to_ccy: str
    rate: Decimal
    path: list[str]                                   # 如 ["HKD", "USD", "CNY"]
    legs: list[dict] = field(default_factory=list)    # 每段：pair/rate/rate_date/source/received_at/inverse/stale_days
    oldest_rate_date: str | None = None
    max_stale_days: int = 0


def resolve(conn: sqlite3.Connection, from_ccy: str, to_ccy: str, on: date, *, max_stale_days: int = 4) -> FxResolution:
    a, b = from_ccy.upper(), to_ccy.upper()
    if a == b:
        return FxResolution(a, b, Decimal(1), [a], [], None, 0)
    try:
        return _via(conn, a, b, [a, b], on, max_stale_days)
    except FxUnavailable:
        if PIVOT in (a, b):
            raise
    return _via(conn, a, b, [a, PIVOT, b], on, max_stale_days)


def _via(conn, a: str, b: str, path: list[str], on: date, max_stale_days: int) -> FxResolution:
    rate = Decimal(1)
    legs = []
    for x, y in zip(path, path[1:], strict=False):
        r, info = get_rate(conn, x + y, on, max_stale_days=max_stale_days)
        rate *= r
        ev = conn.execute("SELECT event_at FROM fx_rate WHERE pair=? AND rate_date=? AND source=? ORDER BY version DESC LIMIT 1",
                          (info["pair"], info["rate_date"], info["source"])).fetchone()
        legs.append({"from": x, "to": y, "rate": str(r), "event_at": ev["event_at"] if ev else None, **info})
    dates = [leg["rate_date"] for leg in legs]
    return FxResolution(a, b, rate, path, legs, min(dates), max(leg["stale_days"] for leg in legs))


def describe_legs(res: FxResolution) -> str:
    """人读的来源说明：`USDHKD 2026-03-05（yfinance，收到 …）`。"""
    if not res.legs:
        return "同币种，无需换算"
    return "；".join(f"{leg['pair']}{'（反向）' if leg['inverse'] else ''} {leg['rate_date']} 来源 {leg['source']}" for leg in res.legs)
