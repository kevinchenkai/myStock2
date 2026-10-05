"""指标字典（ADR 0002、方案 §6A.5）。分母为 0 显示「不可用」，不显示 0%。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mystock2.scoreboard.types import DayResult


@dataclass
class Summary:
    e0: Decimal
    days_planned: int
    days_ok: int
    unknown_days: int
    paused_days: int
    ambiguous_days: int
    cumulative_return: Decimal | None        # R(T)=(E_T−E0)/E0（最后一个 OK 日）
    max_drawdown: Decimal | None             # 权益相对历史峰值的最大下降（比例）
    turnover: Decimal | None                 # Σ成交额 / 平均权益
    avg_exposure: Decimal | None             # 持仓市值/权益 的时间平均
    fills: int
    fees_cum: Decimal
    coverage: Decimal | None                 # OK 日 / 计划日

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}


def daily_returns(results: list[DayResult], e0: Decimal) -> dict:
    """r_t=(E_t−E_{t−1})/E0，仅在相邻两日都是 OK 时定义；首日以 E0 为前值。UNKNOWN/PAUSED 日不产生收益（不记零）。"""
    out: dict = {}
    prev = e0
    prev_ok = True
    for r in results:
        if r.status != "OK":
            prev_ok = False
            continue
        if prev_ok:
            out[r.date] = (r.equity - prev) / e0
        prev, prev_ok = r.equity, True
    return out


def _avg_exposure(ok: list[DayResult]) -> Decimal | None:
    xs = [r.position_value / r.equity for r in ok if r.equity and r.position_value is not None]
    return sum(xs, Decimal(0)) / len(xs) if xs else None


def summarize(results: list[DayResult], e0: Decimal) -> Summary:
    ok = [r for r in results if r.status == "OK"]
    eq = [r.equity for r in ok]
    peak, mdd = e0, Decimal(0)
    for e in eq:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak) if peak > 0 else mdd
    avg_eq = sum(eq, Decimal(0)) / len(eq) if eq else None
    return Summary(
        e0=e0, days_planned=len(results), days_ok=len(ok), unknown_days=sum(r.status == "UNKNOWN" for r in results),
        paused_days=sum(r.status == "PAUSED" for r in results), ambiguous_days=sum("ambiguous_bar" in r.flags for r in ok),
        cumulative_return=(eq[-1] - e0) / e0 if eq and e0 else None,
        max_drawdown=mdd if eq else None,
        turnover=(sum((r.traded_notional for r in ok), Decimal(0)) / avg_eq) if avg_eq else None,
        avg_exposure=_avg_exposure(ok),
        fills=sum(len(r.fills) for r in ok), fees_cum=sum((r.fees_day for r in ok), Decimal(0)),
        coverage=Decimal(len(ok)) / Decimal(len(results)) if results else None,
    )
