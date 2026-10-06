"""持仓估值：用**未复权**收盘价（实施方案 §6A.5）。复权价只用于特征，绝不进权益。

只取 `quality='ok'` 的日线（partial/stale 不当收盘价）；缺行情返回 None 与原因——「不可用」，不记零、不拿上一日冒充当日。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.timeutil import ensure_utc
from mystock2.instruments.code_map import market_of
from mystock2.market.bars import get_daily
from mystock2.web.common import dec_or_none

LOOKBACK_DAYS = 20


@dataclass(frozen=True)
class Price:
    code: str
    close: Decimal | None            # 未复权收盘价；缺失为 None
    session_date: str | None
    stale: bool                      # 早于「应有的最近一个已收盘交易日」
    expected_session: str | None
    event_at: str | None
    received_at: str | None
    reason: str | None = None


def expected_session(market: str, now: datetime) -> date | None:
    """最近一个已收盘（close_utc ≤ now）的交易日；日历覆盖范围外返回 None。"""
    now = ensure_utc(now)
    today = now.date()
    try:
        days = cal.session_days(market, today - timedelta(days=10), today)
        done = [d for d in days if cal.session(market, d).close_utc <= now]
    except cal.CalendarError:
        return None
    return done[-1] if done else None


def latest_close(conn: sqlite3.Connection, code: str, now: datetime) -> Price:
    now = ensure_utc(now)
    market = market_of(code)
    exp = expected_session(market, now)
    rows = get_daily(conn, code, now.date() - timedelta(days=LOOKBACK_DAYS), now.date())
    ok = [r for r in rows if r["quality"] == "ok" and dec_or_none(r["close"], positive=True) is not None]     # 脏收盘价当缺失（陈旧标记会显示出来）
    if not ok:
        why = "近 %d 日无行情" % LOOKBACK_DAYS if not rows else "近 %d 日没有可用的终值收盘价（partial/stale 或数值无效，不作收盘价）" % LOOKBACK_DAYS
        return Price(code, None, None, False, exp.isoformat() if exp else None, None, None, why)
    r = ok[-1]
    stale = bool(exp and r["session_date"] < exp.isoformat())
    return Price(code, dec_or_none(r["close"]), r["session_date"], stale, exp.isoformat() if exp else None, r["event_at"], r["received_at"])


def closes_by_date(conn: sqlite3.Connection, code: str, start: date, end: date) -> dict[str, Decimal]:
    """区间内每个交易日的未复权收盘价（仅 quality='ok'）；缺失的日子不出现在结果里。"""
    out = {}
    for r in get_daily(conn, code, start, end):
        c = dec_or_none(r["close"], positive=True) if r["quality"] == "ok" else None
        if c is not None:
            out[r["session_date"]] = c
    return out
