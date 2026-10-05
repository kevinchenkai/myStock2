"""human_actual：把名单内的**真实成交原样记账**为一条权益序列（描述性证据，不撮合、不适用 exec-v1）。

- 起点：预算 B 的现金＋`D0` 时点账本里属于名单的持仓；之后名单内真实成交的时间、价格、数量**原样**入账，
  现金允许为负（现实中资金可能来自袋外），费用取实际费用事件（归属这些成交的 FEE/TAX）。
- 估值同样用未复权收盘价；缺收盘价 → 当日 UNKNOWN（不记零）。
- 这条线**不进入正式同协议比较**，页面须与模拟线分区呈现（方案 §1 要点 2、§6A.9）。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc, to_market_time
from mystock2.ledger.projection import effective_events, project
from mystock2.scoreboard.engine import MarketData
from mystock2.scoreboard.types import DayResult, SimFill


def human_actual_series(conn_ledger: sqlite3.Connection, md: MarketData, *, account_id: str, market: str, currency: str, codes: set[str],
                        budget: Decimal, d0: date, sessions: list[date]) -> list[DayResult]:
    d0_end = cal.session(market, d0).close_utc
    proj = project(conn_ledger, account_id, as_of=d0_end)
    qty = {c: proj.positions.get(c, Decimal(0)) for c in codes}
    qty = {c: q for c, q in qty.items() if q}
    cash = budget
    events = [e for e in effective_events(conn_ledger, account_id) if ensure_utc(e["event_at"]) > d0_end]
    fill_ids = {e["ref_deal_id"] for e in events if e["event_type"] == "FILL" and e["code"] in codes}
    by_day: dict[date, list] = {}
    for e in events:
        local = to_market_time(e["event_at"], market).date()
        by_day.setdefault(max(local, sessions[0]) if sessions else local, []).append(e)    # D0 收盘后到首个交易日之间的事件并入首日
    out: list[DayResult] = []
    for day in sessions:
        res = DayResult(day, "OK")
        for seq, e in enumerate(by_day.get(day, []), start=1):
            at = ensure_utc(e["event_at"])
            if e["event_type"] == "FILL" and e["code"] in codes:
                q, c = dec(e["qty_delta"]), dec(e["cash_delta"])
                qty[e["code"]] = qty.get(e["code"], Decimal(0)) + q
                cash += c
                res.traded_notional += abs(c)
                res.fills.append(SimFill(day, seq, e["code"], "BUY" if q > 0 else "SELL", abs(q), dec(e["price"]), Decimal(0), at))
            elif e["event_type"] in ("FEE", "TAX") and e["ref_deal_id"] in fill_ids:
                cash += dec(e["cash_delta"])
                res.fees_day += -dec(e["cash_delta"])
        pv, missing = Decimal(0), None
        for code, q in qty.items():
            if q == 0:
                continue
            px = md.close(code, day)
            if px is None:
                missing = code
                break
            pv += q * px
        if missing:
            out.append(DayResult(day, "UNKNOWN", flags=[f"missing_close:{missing}"]))
            continue
        res.equity, res.cash, res.unsettled, res.position_value = cash + pv, cash, Decimal(0), pv
        res.positions = {c: q for c, q in qty.items() if q}
        res.flags.append("descriptive_actual")
        out.append(res)
    return out
