"""`MarketData` 的数据库实现（只读连接）：小时线、会话完整性、未复权收盘价。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc, iso_utc
from mystock2.instruments.code_map import market_of
from mystock2.scoreboard.types import HBar

GAP_TOLERANCE = timedelta(minutes=1)


class DbMarketData:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def hourly(self, code: str, day: date) -> list[HBar]:
        m = market_of(code)
        s = cal.session(m, day)
        rows = self.conn.execute(
            "SELECT * FROM quote_hourly WHERE code=? AND bar_start>=? AND bar_start<? AND complete=1 ORDER BY bar_start, received_at, version",
            (code, iso_utc(s.open_utc), iso_utc(s.close_utc))).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for r in rows:
            latest[r["bar_start"]] = r
        return [HBar(ensure_utc(r["bar_start"]), ensure_utc(r["bar_end"]), dec(r["open"]), dec(r["high"]), dec(r["low"]), dec(r["close"]),
                     int(dec(r["volume"])) if r["volume"] is not None else None) for _, r in sorted(latest.items())]

    def session_complete(self, code: str, day: date) -> bool:
        """bar 是否覆盖整个常规时段 [open, close]（午休间隙除外，其余间隙 ≤ 1 分钟）。"""
        m = market_of(code)
        s = cal.session(m, day)
        bars = self.hourly(code, day)
        if not bars:
            return False
        if bars[0].start - s.open_utc > GAP_TOLERANCE or s.close_utc - bars[-1].end > GAP_TOLERANCE:
            return False
        for a, b in zip(bars, bars[1:], strict=False):
            gap = b.start - a.end
            if gap <= GAP_TOLERANCE:
                continue
            in_break = (s.break_start_utc is not None and a.end >= s.break_start_utc - GAP_TOLERANCE and b.start <= s.break_end_utc + GAP_TOLERANCE)
            if not in_break:
                return False
        return True

    def close(self, code: str, day: date) -> Decimal | None:
        r = self.conn.execute(
            "SELECT close FROM quote_daily WHERE code=? AND session_date=? AND quality='ok' ORDER BY version DESC, received_at DESC LIMIT 1",
            (code, day.isoformat())).fetchone()
        return dec(r["close"]) if r else None
