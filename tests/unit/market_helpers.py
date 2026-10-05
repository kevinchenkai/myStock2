"""行情测试的合成数据（合成测试值）。"""
from __future__ import annotations

import random
from datetime import date, datetime, timezone
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.market.bars import DailyBar

UTC = timezone.utc


def make_db(tmp_path):
    p = tmp_path / "m.db"
    dbmod.migrate(p)
    return p


def writers(tmp_path):
    p = make_db(tmp_path)
    return dbmod.connect_writer(p, "market"), dbmod.connect_writer(p, "forecast"), dbmod.connect_writer(p, "instruments"), p


def synth_bars(code: str, n: int, end: date, seed: int = 1, vol: float = 0.02, start_px: float = 100.0, with_adj: bool = True) -> list[DailyBar]:
    """生成 n 根以 end 结尾的合成日线（随机游走，固定 seed）。日期取日历内交易日。"""
    market = "HK" if code.startswith("HK.") else "US"
    days = cal.session_days(market, date(2020, 1, 2), end)[-n:]
    rnd = random.Random(seed)
    px, out = start_px, []
    for d in days:
        o = px * (1 + rnd.gauss(0, vol / 3))
        c = o * (1 + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append(DailyBar(code, d, f"{o:.4f}", f"{h:.4f}", f"{lo:.4f}", f"{c:.4f}", f"{c:.4f}" if with_adj else None, "1000000"))
        px = c
    return out


def after_close(d: date, market: str = "US", hours: int = 2) -> datetime:
    return cal.session(market, d).close_utc.replace(tzinfo=UTC) + __import__("datetime").timedelta(hours=hours)


D = Decimal
