"""运行成本后的经济增量表（SB-06、R26）：交易费用后主表之外，另列扣除可归属持续运行成本的效果。

成本分配方法须预注册；开发投入单列，不摊入短期窗口。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class EconomicRow:
    gross_diff: Decimal            # 费用后（交易费用）累计收益差，占 E0 的比例
    running_cost: Decimal          # 窗口内可归属运行成本（同币种金额）
    e0: Decimal
    net_diff: Decimal              # 扣除运行成本后的收益差（占 E0 比例）
    cost_ratio: Decimal            # 运行成本占 E0 的比例


def after_running_costs(cumulative_diff: Decimal, costs_by_kind: dict[str, Decimal], e0: Decimal) -> EconomicRow:
    """cumulative_diff 为 ΣΔ_t（比例）；costs_by_kind 为同币种金额。"""
    total = sum(costs_by_kind.values(), Decimal(0))
    ratio = total / e0
    return EconomicRow(cumulative_diff, total, e0, cumulative_diff - ratio, ratio)
