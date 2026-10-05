"""港股/美股交易日历（只读，失败关闭）。

数据来自 V1 冻结日历（见 data/calendars/PROVENANCE.md），覆盖 2020-01-01 ~ 2027-12-31。
- 只使用 open/close/break_start/break_end；V1 的 deadline/final_at 是 V1 协议字段，V2 不用。
- 覆盖范围之外的查询一律报错（不推断），到期前 60 天给出告警。
- 缺行情不能用来推断休市；半日市以 close 与常规时间之差识别（见 `Session.is_half_day`）。
- 项目截止（HK 08:30 HKT、US 09:00 ET）是协议参数，由 `project_deadline()` 按各市场本地时区与夏令时换算。
"""
from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from mystock2.core.timeutil import MARKET_TZ, ensure_utc, market_local_to_utc, utc_now

CALENDAR_VERSION = "pmc-5.1.3-xhkg-4.11.1-2020-2027-cas-weather-v3"
COVERAGE_START = date(2020, 1, 1)
COVERAGE_END = date(2027, 12, 31)
_DATA_DIR = Path(__file__).parent / "data" / "calendars"

# 默认项目截止（本地时间）；协议可覆盖（实施方案 §6.5）。
DEFAULT_DEADLINE_LOCAL = {"HK": (8, 30), "US": (9, 0)}
# 常规收盘（本地），用于识别半日市
_REGULAR_CLOSE_LOCAL = {"HK": (16, 10), "US": (16, 0)}


class CalendarError(ValueError):
    """日历不可用（覆盖范围外、未知市场、非交易日查询等）。"""


@dataclass(frozen=True)
class Session:
    market: str
    day: date
    open_utc: datetime
    close_utc: datetime
    break_start_utc: datetime | None
    break_end_utc: datetime | None

    @property
    def is_half_day(self) -> bool:
        h, m = _REGULAR_CLOSE_LOCAL[self.market]
        regular = market_local_to_utc(self.market, self.day.year, self.day.month, self.day.day, h, m)
        # HK 收盘竞价使常规收盘存在 ±10 分钟差异：以「早于常规收盘 30 分钟以上」判定半日市
        return self.close_utc < regular - timedelta(minutes=30)


def _market(market: str) -> str:
    m = market.upper()
    if m not in MARKET_TZ:
        raise CalendarError(f"未知市场：{market!r}")
    return m


def _as_date(day: date | str) -> date:
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    return date.fromisoformat(str(day)[:10])


def _check_range(day: date) -> None:
    if not COVERAGE_START <= day <= COVERAGE_END:
        raise CalendarError(
            f"日期 {day} 超出已核验日历范围 {COVERAGE_START}~{COVERAGE_END}；"
            "请按交易所公告核验并新增日历版本后重试（失败关闭，不推断）"
        )


@lru_cache(maxsize=4)
def _load(market: str) -> dict[date, Session]:
    path = _DATA_DIR / f"{market}.csv"
    out: dict[date, Session] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            d = date.fromisoformat(row["date"])
            out[d] = Session(
                market=market,
                day=d,
                open_utc=ensure_utc(row["open"]),
                close_utc=ensure_utc(row["close"]),
                break_start_utc=ensure_utc(row["break_start"]) if row.get("break_start") else None,
                break_end_utc=ensure_utc(row["break_end"]) if row.get("break_end") else None,
            )
    return out


@lru_cache(maxsize=4)
def _days(market: str) -> tuple[date, ...]:
    return tuple(sorted(_load(market)))


def is_session(market: str, day: date | str) -> bool:
    m, d = _market(market), _as_date(day)
    _check_range(d)
    return d in _load(m)


def session(market: str, day: date | str) -> Session:
    m, d = _market(market), _as_date(day)
    _check_range(d)
    try:
        return _load(m)[d]
    except KeyError as exc:
        raise CalendarError(f"{m} {d} 不是交易日") from exc


def session_days(market: str, start: date | str, end: date | str) -> list[date]:
    m, s, e = _market(market), _as_date(start), _as_date(end)
    _check_range(s)
    _check_range(e)
    ds = _days(m)
    return list(ds[bisect_left(ds, s): bisect_right(ds, e)])


def next_session(market: str, day: date | str) -> date:
    """严格晚于 day 的下一个交易日。"""
    m, d = _market(market), _as_date(day)
    _check_range(d)
    ds = _days(m)
    i = bisect_right(ds, d)
    if i >= len(ds):
        raise CalendarError(f"{m} 在 {d} 之后没有已核验的交易日（超出日历覆盖）")
    return ds[i]


def prev_session(market: str, day: date | str) -> date:
    """严格早于 day 的上一个交易日。"""
    m, d = _market(market), _as_date(day)
    _check_range(d)
    ds = _days(m)
    i = bisect_left(ds, d)
    if i == 0:
        raise CalendarError(f"{m} 在 {d} 之前没有已核验的交易日")
    return ds[i - 1]


def project_deadline(market: str, day: date | str, local_hm: tuple[int, int] | None = None) -> datetime:
    """目标交易日的项目截止（UTC）：本地时间换算，自动处理夏令时。目标日必须是交易日。"""
    m = _market(market)
    s = session(m, day)
    h, mi = local_hm or DEFAULT_DEADLINE_LOCAL[m]
    return market_local_to_utc(m, s.day.year, s.day.month, s.day.day, h, mi)


def calendar_days_left(now: datetime | None = None) -> int:
    return (COVERAGE_END - ensure_utc(now or utc_now()).date()).days


def calendar_warnings(now: datetime | None = None) -> list[dict]:
    left = calendar_days_left(now)
    if left >= 60:
        return []
    return [
        {
            "status": "calendar_expiring" if left >= 0 else "calendar_expired",
            "calendar_days_left": left,
            "calendar_end": COVERAGE_END.isoformat(),
            "calendar_version": CALENDAR_VERSION,
        }
    ]
