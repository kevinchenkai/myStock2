"""行为指标（RP-02）：描述性，每项附样本量与口径；样本不足显示「不足」，不显示 0，不下确定性结论。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from mystock2.core.money import dec
from mystock2.replay.cards import ReviewCard
from mystock2.replay.rounds import Round

MIN_SAMPLE = 5


@dataclass
class Metric:
    name: str
    value: str | None          # None＝不足
    n: int
    definition: str

    @property
    def display(self) -> str:
        return "不足" if self.value is None else self.value


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs, Decimal(0)) / len(xs) if xs else None


def _metric(name: str, xs: list, definition: str, fn) -> Metric:
    if len(xs) < MIN_SAMPLE:
        return Metric(name, None, len(xs), definition + f"（样本 {len(xs)} < {MIN_SAMPLE}，不足）")
    return Metric(name, str(fn(xs)), len(xs), definition)


def behavior_metrics(cards: list[ReviewCard], rounds: list[Round]) -> list[Metric]:
    buys = [dec(c.execution["range_position"]) for c in cards if c.side == "BUY" and c.execution.get("range_position") is not None]
    sells = [dec(c.execution["range_position"]) for c in cards if c.side == "SELL" and c.execution.get("range_position") is not None]
    sold_up = [c for c in cards if c.side == "SELL" and c.outcome.get("horizons", {}).get(5) is not None]
    adds = [c for c in cards if c.side == "BUY" and c.inventory_before > 0]
    closed = [r for r in rounds if r.pnl_after_fees is not None]
    wins = [r.pnl_after_fees for r in closed if r.pnl_after_fees > 0]
    losses = [-r.pnl_after_fees for r in closed if r.pnl_after_fees < 0]
    out = [
        _metric("买入执行质量（区间位置均值，越小越好）", buys, "买入成交价在当日 [最低, 最高] 区间内的相对位置的均值", _mean),
        _metric("卖出执行质量（区间位置均值，越小越好）", sells, "卖出成交价取反后的相对位置均值（越接近当日最高越小）", _mean),
        _metric("卖出后 5 个交易日继续上涨的比例", sold_up, "卖出后第 5 个交易日（复权）价格高于卖价的卖出笔数占比",
                lambda xs: Decimal(sum(1 for c in xs if dec(c.outcome["horizons"][5]) > 0)) / len(xs)),
    ]
    out.append(_metric("已持仓时加仓的买入笔数", adds, "成交前已有库存的买入笔数（加仓频率的分子；亏损仓加仓需成本信息，见诊断回合）", lambda xs: Decimal(len(xs))))
    out.append(_metric("平均持有天数（已平仓回合）", [r.holding_days for r in rounds], "FIFO 诊断回合的持有自然日数均值", lambda xs: Decimal(sum(xs)) / len(xs)))
    out.append(_metric("持有天数中位数（已平仓回合）", [r.holding_days for r in rounds], "FIFO 诊断回合的持有自然日数中位数", lambda xs: Decimal(str(median(xs)))))
    out.append(_metric("已平仓回合胜率（描述，不评判）", closed, "费用后盈利回合数 / 有成本证据的已平仓回合数", lambda xs: Decimal(len(wins)) / len(xs)))
    out.append(_metric("盈亏比（平均盈利/平均亏损，描述）", closed if wins and losses else [], "平均盈利回合盈利 / 平均亏损回合亏损绝对值",
                       lambda xs: (sum(wins, Decimal(0)) / len(wins)) / (sum(losses, Decimal(0)) / len(losses))))
    return out
