"""记分牌的核心数据结构（纯数据，无 I/O）。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mystock2.core.money import dec, to_db

BUY, SELL = "BUY", "SELL"


@dataclass(frozen=True)
class HBar:
    start: object          # datetime (UTC)
    end: object
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None


@dataclass(frozen=True)
class SimOrder:
    """日内单。limit_price=None 表示开盘市价单（仅买入持有建仓使用）。"""

    line_id: str
    code: str
    side: str
    qty: int
    limit_price: Decimal | None = None
    tag: str = ""


@dataclass
class Lot:
    qty: Decimal
    unit_cost: Decimal          # 含买入费用的单位成本
    acquired: date


@dataclass
class LineState:
    """某条线在某币种袋内的状态（§6A.1：每条线只读自己的状态）。"""

    currency: str
    cash: Decimal = Decimal(0)                                   # 含未结算回款
    unsettled: list[tuple[date, Decimal]] = field(default_factory=list)
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    fees_cum: Decimal = Decimal(0)

    def qty(self, code: str) -> Decimal:
        return sum((lot.qty for lot in self.lots.get(code, [])), Decimal(0))

    def avg_cost(self, code: str) -> Decimal | None:
        q = self.qty(code)
        if q == 0:
            return None
        return sum((lot.qty * lot.unit_cost for lot in self.lots[code]), Decimal(0)) / q

    def holding_age_days(self, code: str, today: date) -> int | None:
        lots = self.lots.get(code)
        return (today - min(lot.acquired for lot in lots)).days if lots else None

    def unsettled_total(self) -> Decimal:
        return sum((a for _, a in self.unsettled), Decimal(0))

    def tradable_cash(self) -> Decimal:
        return self.cash - self.unsettled_total()

    def copy(self) -> "LineState":
        return LineState(self.currency, self.cash, list(self.unsettled), {c: [Lot(lot.qty, lot.unit_cost, lot.acquired) for lot in ls] for c, ls in self.lots.items()}, self.fees_cum)

    def to_dict(self) -> dict:
        return {
            "currency": self.currency, "cash": to_db(self.cash), "fees_cum": to_db(self.fees_cum),
            "unsettled": [[d.isoformat(), to_db(a)] for d, a in sorted(self.unsettled)],
            "lots": {c: [[to_db(lot.qty), to_db(lot.unit_cost), lot.acquired.isoformat()] for lot in ls] for c, ls in sorted(self.lots.items())},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LineState":
        return cls(d["currency"], dec(d["cash"]), [(date.fromisoformat(x), dec(a)) for x, a in d["unsettled"]],
                   {c: [Lot(dec(q), dec(u), date.fromisoformat(a)) for q, u, a in ls] for c, ls in d["lots"].items()}, dec(d["fees_cum"]))

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class SimFill:
    date: date
    seq: int
    code: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    bar_start: object | None
    ambiguous: bool = False
    note: str = ""


@dataclass
class DayResult:
    date: date
    status: str                              # OK | UNKNOWN | PAUSED
    equity: Decimal | None = None
    cash: Decimal | None = None
    unsettled: Decimal | None = None
    position_value: Decimal | None = None
    fees_day: Decimal = Decimal(0)
    positions: dict[str, Decimal] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    fills: list[SimFill] = field(default_factory=list)
    rejected: list[tuple[SimOrder, str]] = field(default_factory=list)
    traded_notional: Decimal = Decimal(0)


@dataclass(frozen=True)
class ExecProtocol:
    """exec-v1（ADR 0002）。改动任何参数＝新协议版本。"""

    version: str = "exec-v1"
    max_participation: Decimal = Decimal("0.10")
    fee_multiplier: Decimal = Decimal(1)       # 敏感性：费用×2
    slippage_bps: Decimal = Decimal(0)         # 敏感性：对成交价不利方向的滑点

    def as_dict(self) -> dict:
        return {"version": self.version, "max_participation": to_db(self.max_participation),
                "fee_multiplier": to_db(self.fee_multiplier), "slippage_bps": to_db(self.slippage_bps)}


def sensitivity_variants(base: ExecProtocol) -> dict[str, ExecProtocol]:
    """必做敏感性（SB-05）：费用×2、不利滑点、更低参与率；与主表并列呈现，**不得为让结果好看改用更乐观的撮合**。
    「剔除双边歧义日」由 `stats.paired_diff(..., exclude_dates=...)` 完成。"""
    from dataclasses import replace
    return {
        "fee_x2": replace(base, fee_multiplier=base.fee_multiplier * 2),
        "slippage_10bps": replace(base, slippage_bps=base.slippage_bps + Decimal(10)),
        "participation_half": replace(base, max_participation=base.max_participation / 2),
    }
