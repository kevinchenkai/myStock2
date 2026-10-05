"""保守限价撮合（ADR 0002 / exec-v1）。纯函数。

买：bar.low < 限价 才可成交；卖：bar.high > 限价；触价（相等）不成交。
成交价恒为限价（不因跳空改善）。每 bar 成交量不超过 max_participation × volume，多 bar 累计，剩余作废。
市价单（limit_price=None，仅买入持有）：以首根完整 bar 的开盘价一次成交，不受参与率限制。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from mystock2.scoreboard.types import BUY, SELL, ExecProtocol, HBar, SimOrder


@dataclass(frozen=True)
class BarFill:
    bar: HBar
    qty: int
    price: Decimal


@dataclass(frozen=True)
class Match:
    fills: list[BarFill]
    remaining: int
    status: str                      # filled | partial | unfilled | unknown
    crossed_bars: tuple              # 满足触价条件的 bar 起点（用于双边歧义判断）


def crosses(side: str, limit: Decimal, bar: HBar) -> bool:
    return bar.low < limit if side == BUY else bar.high > limit


def _slip(price: Decimal, side: str, bps: Decimal) -> Decimal:
    adj = price * bps / Decimal(10000)
    return price + adj if side == BUY else price - adj


def match_order(order: SimOrder, bars: list[HBar], protocol: ExecProtocol, *, lot_size: int = 1, complete: bool = True) -> Match:
    """bars：当日完整小时线，按时间升序。complete：是否覆盖整个常规时段。"""
    if order.qty <= 0 or order.qty % lot_size != 0:
        raise ValueError("订单数量必须是整手的正数")
    remaining = order.qty
    fills: list[BarFill] = []
    crossed: list = []
    if order.limit_price is None:                      # 开盘市价单
        if not bars:
            return Match([], remaining, "unknown", ())
        b = bars[0]
        return Match([BarFill(b, order.qty, _slip(b.open, order.side, protocol.slippage_bps))], 0, "filled", ())
    unknown_volume = False
    for b in bars:
        if not crosses(order.side, order.limit_price, b):
            continue
        crossed.append(b.start)
        if remaining == 0:
            continue
        if b.volume is None:
            unknown_volume = True
            continue
        cap = int(math.floor(protocol.max_participation * b.volume / lot_size)) * lot_size
        q = min(remaining, cap)
        if q > 0:
            fills.append(BarFill(b, q, _slip(order.limit_price, order.side, protocol.slippage_bps)))
            remaining -= q
    if remaining == 0:
        status = "filled"
    elif unknown_volume or not complete:
        status = "unknown"                              # 缺失的 bar/容量里可能已成交：无法确定
    elif fills:
        status = "partial"
    else:
        status = "unfilled"
    return Match(fills, remaining, status, tuple(crossed))


def opposite(side: str) -> str:
    return SELL if side == BUY else BUY
