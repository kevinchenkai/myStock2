"""诊断回合（FIFO，RP-03）：把真实成交按先进先出配对成回合，用于行为诊断，**不是**账本收益口径。

- 开账日之前的库存（期初事件）成本未知 → 对应回合标 `cost_unknown`，不给盈亏。
- 费用取实际费用事件（归属成交的 FEE/TAX），按成交数量比例分摊到回合；缺失费用时盈亏标 `fees_missing`（费用前）。
- 平仓回合＝某批买入被卖出完毕；未平仓部分不算胜负（仍显示持仓风险，不当作已胜已负）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mystock2.core.money import dec


@dataclass
class Round:
    code: str
    open_date: date
    close_date: date
    qty: Decimal
    cost_unit: Decimal | None
    exit_unit: Decimal
    fees: Decimal                      # 分摊的买卖费用（正数）
    pnl_after_fees: Decimal | None     # cost 未知时 None
    holding_days: int
    flags: list[str] = field(default_factory=list)


def build_rounds(fills: list[dict], fees_by_deal: dict[str, Decimal]) -> tuple[list[Round], dict[str, list[dict]]]:
    """fills：按时间升序的真实成交 dict（code、date、qty(带符号)、price、deal_id、opening=bool、currency）。

    返回 (已平仓回合, 剩余未平仓批次 {code: [lot...]})。
    """
    lots: dict[str, list[dict]] = {}
    rounds: list[Round] = []
    for f in fills:
        code, q = f["code"], dec(f["qty"])
        if q > 0:
            fee = fees_by_deal.get(f["deal_id"], Decimal(0))
            lots.setdefault(code, []).append({
                "qty": q, "orig_qty": q, "date": f["date"], "cost": None if f.get("opening") else dec(f["price"]),
                "fee_unit": (fee / q) if q else Decimal(0), "fee_known": f["deal_id"] in fees_by_deal or bool(f.get("opening")),
            })
            continue
        left = -q
        exit_px = dec(f["price"])
        sell_fee_unit = fees_by_deal.get(f["deal_id"], Decimal(0)) / (-q)
        sell_fee_known = f["deal_id"] in fees_by_deal
        while left > 0 and lots.get(code):
            lot = lots[code][0]
            take = min(lot["qty"], left)
            lot["qty"] -= take
            left -= take
            flags = []
            if lot["cost"] is None:
                flags.append("cost_unknown")
            if not (lot["fee_known"] and sell_fee_known):
                flags.append("fees_missing")
            fees = (lot["fee_unit"] + sell_fee_unit) * take
            pnl = None if lot["cost"] is None else (exit_px - lot["cost"]) * take - fees
            rounds.append(Round(code, lot["date"], f["date"], take, lot["cost"], exit_px, fees, pnl, (f["date"] - lot["date"]).days, flags))
            if lot["qty"] == 0:
                lots[code].pop(0)
        if left > 0:
            rounds.append(Round(code, f["date"], f["date"], left, None, exit_px, Decimal(0), None, 0, ["sold_without_inventory"]))
    return rounds, {c: ls for c, ls in lots.items() if ls}
