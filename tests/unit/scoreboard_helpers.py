"""记分牌测试的合成行情与辅助（合成测试值）。"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.ledger.fees import FeeRule
from mystock2.ledger.settlement import SettlementRule
from mystock2.scoreboard.types import ExecProtocol, HBar, LineState

UTC = timezone.utc
D = Decimal
CODE, CODE2 = "US.NVDA", "US.TSLA"
DAYS = cal.session_days("US", date(2026, 3, 2), date(2026, 3, 13))     # 10 个交易日（周一至周五 ×2）
PROTO = ExecProtocol()
RULES = [FeeRule("syn", "US", "ANY", "order", "USD", pct_fee=D("0.001"), min_fee=D("1"))]
SETTLE1 = SettlementRule("US", 1)


class FakeMD:
    """按 (code, day) 配置小时线、完整性与收盘价。"""

    def __init__(self):
        self.bars: dict[tuple[str, date], list[HBar]] = {}
        self.complete: dict[tuple[str, date], bool] = {}
        self.closes: dict[tuple[str, date], Decimal | None] = {}

    def set_day(self, code, day, hourly, close=None, complete=True):
        s = cal.session("US", day)
        bars = []
        for i, (o, h, lo, c, v) in enumerate(hourly):
            start = s.open_utc + timedelta(hours=i)
            bars.append(HBar(start, start + timedelta(hours=1), D(str(o)), D(str(h)), D(str(lo)), D(str(c)), v))
        self.bars[(code, day)] = bars
        self.complete[(code, day)] = complete
        self.closes[(code, day)] = D(str(close if close is not None else hourly[-1][3]))

    def flat(self, code, days, px, volume=1_000_000):
        for d in days:
            self.set_day(code, d, [(px, px + 1, px - 1, px, volume)] * 6, close=px)

    def hourly(self, code, day):
        return list(self.bars.get((code, day), []))

    def session_complete(self, code, day):
        return self.complete.get((code, day), False)

    def close(self, code, day):
        return self.closes.get((code, day))


def state(cash, **lots):
    from mystock2.scoreboard.types import Lot
    st = LineState("USD", D(str(cash)))
    for code, (qty, unit, acquired) in lots.items():
        st.lots[code.replace("_", ".")] = [Lot(D(str(qty)), D(str(unit)), acquired)]
    return st
