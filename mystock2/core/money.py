"""金额与数量：全程 Decimal，SQLite 中以十进制字符串存储（实施方案 §3.2、LG-08）。

禁止 float 进入账本：`dec()` 对 float 直接报错，避免 0.1+0.2 一类累计误差。
跨币种不能相加：`Money` 做加减时强制币种一致（AGENTS.md「金额注明币种，跨币种不直接相加」）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Union

Number = Union[str, int, Decimal]

_PREC = 40  # 足以容纳任何券商金额的精度


class MoneyError(ValueError):
    pass


def dec(value: Number) -> Decimal:
    """把字符串/整数/Decimal 转为 Decimal；float 与 bool 一律拒绝；NaN/Inf 拒绝。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError(f"禁止使用 {type(value).__name__} 表示金额/数量：{value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (str, int)):
        try:
            d = Decimal(value.strip() if isinstance(value, str) else value)
        except InvalidOperation as exc:
            raise MoneyError(f"无法解析为十进制数：{value!r}") from exc
    else:
        raise MoneyError(f"不支持的类型 {type(value).__name__}：{value!r}")
    if not d.is_finite():
        raise MoneyError(f"非有限数值：{value!r}")
    return d


def to_db(value: Number) -> str:
    """规范化十进制字符串（无指数、无多余尾零、负零归零），用于入库与哈希。"""
    d = dec(value)
    if d == 0:
        return "0"
    with localcontext() as ctx:
        ctx.prec = _PREC
        text = format(d.normalize(), "f")
    return text


def from_db(text: str) -> Decimal:
    return dec(text)


def quantize(value: Number, step: Number, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """按步长舍入到 step 的整数倍（如 tick、手数）。"""
    d, s = dec(value), dec(step)
    if s <= 0:
        raise MoneyError("步长必须为正")
    with localcontext() as ctx:
        ctx.prec = _PREC
        return (d / s).to_integral_value(rounding=rounding) * s


def floor_to_step(value: Number, step: Number) -> Decimal:
    return quantize(value, step, ROUND_FLOOR)


def ceil_to_step(value: Number, step: Number) -> Decimal:
    return quantize(value, step, ROUND_CEILING)


def round_limit_price(price: Number, tick: Number, side: str) -> Decimal:
    """限价保守舍入到合法 tick：买价向下、卖价向上（实施方案 M5 执行协议）。"""
    side = side.upper()
    if side == "BUY":
        return floor_to_step(price, tick)
    if side == "SELL":
        return ceil_to_step(price, tick)
    raise MoneyError(f"side 必须是 BUY/SELL：{side!r}")


def floor_to_lots(qty: Number, lot_size: int) -> Decimal:
    """数量向下取整到整手。"""
    if lot_size <= 0:
        raise MoneyError("lot_size 必须为正")
    return floor_to_step(qty, lot_size)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", dec(self.amount))
        cur = self.currency.upper() if isinstance(self.currency, str) else ""
        if len(cur) != 3 or not cur.isalpha():
            raise MoneyError(f"币种必须是 3 位字母代码：{self.currency!r}")
        object.__setattr__(self, "currency", cur)

    def _same(self, other: "Money") -> None:
        if not isinstance(other, Money):
            raise MoneyError("只能与 Money 运算")
        if other.currency != self.currency:
            raise MoneyError(f"跨币种不能直接相加/相减：{self.currency} vs {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __str__(self) -> str:
        return f"{to_db(self.amount)} {self.currency}"
