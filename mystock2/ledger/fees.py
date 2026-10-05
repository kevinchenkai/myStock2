"""费用档案与估算（LG-05、T-21）。费率数值属私有，仓库只含模板与合成测试值。

- 聚合层级：`order`（同一订单的多笔成交合并计费，最低费只收一次）、`fill`（每笔成交各自计费）、`period`（账期，不在此估算）。
- 估算费用与实际费用**分列**：估算只用于共同的模型化费用口径（记分牌），不写入账本事件。
- 舍入：按 `round_step` 四舍五入（半偶），封顶 `cap_fee`。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import yaml

from mystock2.core.money import dec, quantize


@dataclass(frozen=True)
class FeeRule:
    profile_id: str
    market: str
    side: str            # BUY / SELL / ANY
    basis: str           # order / fill
    currency: str
    pct_fee: Decimal = Decimal(0)     # 成交额比例（小数，如 0.0003）
    min_fee: Decimal = Decimal(0)
    flat_fee: Decimal = Decimal(0)
    cap_fee: Decimal | None = None
    tax_pct: Decimal = Decimal(0)     # 税（如印花税）按成交额比例，通常只在卖出
    round_step: Decimal = Decimal("0.01")
    valid_from: str = "1970-01-01"
    valid_to: str | None = None


@dataclass(frozen=True)
class FeeEstimate:
    fee: Decimal
    tax: Decimal

    @property
    def total(self) -> Decimal:
        return self.fee + self.tax


def _one(rule: FeeRule, notional: Decimal) -> FeeEstimate:
    fee = notional * rule.pct_fee + rule.flat_fee
    fee = max(fee, rule.min_fee)
    if rule.cap_fee is not None:
        fee = min(fee, rule.cap_fee)
    return FeeEstimate(quantize(fee, rule.round_step, ROUND_HALF_EVEN), quantize(notional * rule.tax_pct, rule.round_step, ROUND_HALF_EVEN))


def estimate(rule: FeeRule, fills: list[tuple[Decimal, Decimal]]) -> FeeEstimate:
    """fills: [(qty, price)]（qty 取绝对值）。basis=order 合并后计一次；basis=fill 逐笔计费后求和。"""
    if rule.basis == "order":
        return _one(rule, sum((abs(q) * p for q, p in fills), Decimal(0)))
    if rule.basis == "fill":
        parts = [_one(rule, abs(q) * p) for q, p in fills]
        return FeeEstimate(sum((x.fee for x in parts), Decimal(0)), sum((x.tax for x in parts), Decimal(0)))
    raise ValueError(f"不支持的计费层级：{rule.basis}")


def select_rule(rules: list[FeeRule], market: str, side: str, on_date: str) -> FeeRule:
    """按市场、方向与生效期选规则：side 精确匹配优先于 ANY，其次取 valid_from 最晚者；无命中报错（不静默为 0）。"""
    hits = [r for r in rules if r.market == market and r.side in (side, "ANY") and r.valid_from <= on_date
            and (r.valid_to is None or on_date < r.valid_to)]
    if not hits:
        raise LookupError(f"没有适用的费用档案：{market} {side} {on_date}")
    return max(hits, key=lambda r: (r.side == side, r.valid_from))


def load_fee_rules(path: str | Path) -> list[FeeRule]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules = []
    for r in data.get("rules", []):
        rules.append(FeeRule(
            profile_id=r["profile_id"], market=r["market"], side=r.get("side", "ANY"), basis=r["basis"], currency=r["currency"],
            pct_fee=dec(str(r.get("pct_fee", "0"))), min_fee=dec(str(r.get("min_fee", "0"))), flat_fee=dec(str(r.get("flat_fee", "0"))),
            cap_fee=dec(str(r["cap_fee"])) if r.get("cap_fee") is not None else None, tax_pct=dec(str(r.get("tax_pct", "0"))),
            round_step=dec(str(r.get("round_step", "0.01"))), valid_from=str(r.get("valid_from", "1970-01-01")),
            valid_to=str(r["valid_to"]) if r.get("valid_to") else None,
        ))
    return rules
