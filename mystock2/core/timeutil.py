"""时间约定（AGENTS.md、实施方案 §1、§5 WP4.2、F07）。

- 存储统一 UTC；展示用 IANA 时区。naive datetime 一律拒绝（时区未知不可当作 UTC）。
- 四类时间：event_at（事件发生）、available_at（协议下可得）、received_at（实际接收）、generated_at（生成完成）；
  前向评价另有 decision_at / data_cutoff / target_session / deadline_at。
- `check_time_chain` 实现 F07 的时间链：received_at <= input_cutoff_at <= generated_at <= frozen_at <= deadline_at。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MARKET_TZ = {"HK": ZoneInfo("Asia/Hong_Kong"), "US": ZoneInfo("America/New_York")}


class TimeError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | str) -> datetime:
    """转成带 UTC 时区的 datetime；naive 输入报错。"""
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise TimeError(f"无法解析时间：{value!r}") from exc
    if not isinstance(value, datetime):
        raise TimeError(f"不是时间类型：{type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TimeError("时间缺少时区信息（拒绝把 naive 时间当作 UTC）")
    return value.astimezone(UTC)


def iso_utc(value: datetime | str) -> str:
    """规范化 UTC 文本：**固定**微秒精度 YYYY-MM-DDTHH:MM:SS.ffffffZ。

    固定精度保证文本的字典序与时间序一致（可变精度时 `…00Z` 与 `…00.100000Z` 的字典序会反过来，
    使 SQL/Python 的字符串比较在同一秒内出错）。入库、哈希与比较一律用本函数。
    """
    d = ensure_utc(value)
    return d.isoformat(timespec="microseconds").replace("+00:00", "Z")


def to_market_time(value: datetime | str, market: str) -> datetime:
    try:
        tz = MARKET_TZ[market.upper()]
    except KeyError as exc:
        raise TimeError(f"未知市场：{market!r}") from exc
    return ensure_utc(value).astimezone(tz)


def market_local_to_utc(market: str, year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """把市场本地时间（如 HK 08:30）转成 UTC；夏令时由 IANA 时区处理。"""
    tz = MARKET_TZ[market.upper()]
    return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(UTC)


@dataclass(frozen=True)
class EvidenceTimes:
    """一个输入/版本的时间链字段。"""

    received_at: datetime
    input_cutoff_at: datetime
    generated_at: datetime
    frozen_at: datetime
    deadline_at: datetime


def check_time_chain(t: EvidenceTimes) -> list[str]:
    """返回违反时间链的原因列表；空列表表示合规。

    received_at <= input_cutoff_at <= generated_at <= frozen_at <= deadline_at
    """
    r = ensure_utc(t.received_at)
    c = ensure_utc(t.input_cutoff_at)
    g = ensure_utc(t.generated_at)
    f = ensure_utc(t.frozen_at)
    d = ensure_utc(t.deadline_at)
    problems = []
    if r > c:
        problems.append("received_after_input_cutoff")
    if c > g:
        problems.append("input_cutoff_after_generated")
    if g > f:
        problems.append("generated_after_frozen")
    if f > d:
        problems.append("frozen_after_deadline")
    return problems
