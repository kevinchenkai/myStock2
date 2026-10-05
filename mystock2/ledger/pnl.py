"""已实现盈亏：移动平均成本法（纯函数，无 I/O；实施方案 §3.3 WP3.3、T-01、§6 不变量 1）。

口径（写死，改动即口径变更）：

- **成本＝移动平均**：买入（含买入费用）并入成本池，卖出按当时平均成本结转；卖出费用冲减卖出收入。
  结果是「费用后」的已实现盈亏；`fees_total` 另列，便于看到费用占比。
- **开账日前没有成本证据**：开账持仓（`OPENING`）的成本只能来自开账快照里的券商成本，且是**估算**；
  快照没有成本时，这部分股份的成本「不可用」。卖出消耗库存时，三类股份（有精确成本／开账估算成本／无成本证据）
  在移动平均下不可区分，按**持股比例**分摊消耗：
    - 消耗到无成本证据的那部分 → 该部分盈亏「不可用」，只记数量与净收入；
    - 平均成本里只要还混有开账估算成本，整笔已知部分的盈亏标「估算」；持仓清零后才恢复「精确」。
- **开账日及以前的成交（`pre_opening`）不产生盈亏**：它们只作描述（与账本和式一致，不变量 1），单列。
- **超卖**（卖出数量超过可追溯库存）：超出部分「不可用」，并告警；不虚构成本。
- 拆股是因子：数量乘 num/den，总成本不变（平均成本自动除以因子）；同一时刻先应用拆股、再处理成交。
- **成交净现金流 ≠ 盈亏**：`trade_net_cashflow` 只是买入为负、卖出为正的现金流水合计，函数名与文档均不称其为盈利。

全程 Decimal；缺失用 `None`，不记零。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Iterable

from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc

OPENING, BUY, SELL, SPLIT = "OPENING", "BUY", "SELL", "SPLIT"
KINDS = (OPENING, BUY, SELL, SPLIT)
_RANK = {SPLIT: 0, OPENING: 1, BUY: 2, SELL: 2}     # 同一时刻：先拆股，再期初，再成交（保持输入相对顺序）

EXACT, ESTIMATED, PARTIAL, UNAVAILABLE = "exact", "estimated", "partial", "unavailable"
QUALITY_TEXT = {EXACT: "精确", ESTIMATED: "估算", PARTIAL: "部分不可用", UNAVAILABLE: "不可用"}


class PnlError(ValueError):
    pass


@dataclass(frozen=True)
class TradeEvent:
    """盈亏计算的输入事件。数量为正；方向由 kind 表示。"""
    kind: str
    code: str
    currency: str
    at: datetime | str
    qty: Decimal = Decimal(0)               # OPENING/BUY/SELL 的股数（正）；SPLIT 不用
    price: Decimal | None = None            # BUY/SELL 的成交价；OPENING 的每股成本估计（无证据则 None）
    fee: Decimal = Decimal(0)               # 归属于该成交的费用与税（≥0）
    ref: str = ""                           # 成交编号等引用
    ratio_num: int = 1                      # SPLIT：1 股变 num/den 股
    ratio_den: int = 1


@dataclass(frozen=True)
class SellPnl:
    ref: str
    code: str
    currency: str
    at: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    net_proceeds: Decimal                   # price×qty − fee
    avg_cost: Decimal | None                # 本笔结转所用的平均成本（每股；无已知成本时 None）
    known_qty: Decimal                      # 有成本证据的数量
    unavailable_qty: Decimal                # 无成本证据（含超卖）的数量
    realized: Decimal | None                # 已知部分的已实现盈亏（费用后）；全部不可用时 None
    quality: str                            # exact / estimated / partial / unavailable
    note: str = ""


@dataclass(frozen=True)
class PreOpeningTrade:
    ref: str
    code: str
    currency: str
    at: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal


@dataclass
class CodePnl:
    code: str
    currency: str
    realized_exact: Decimal = Decimal(0)
    realized_estimated: Decimal = Decimal(0)
    unavailable_qty: Decimal = Decimal(0)           # 已卖出但无成本证据的累计数量
    unavailable_net_proceeds: Decimal = Decimal(0)  # 这部分的净收入（只作参考，不是盈亏）
    fees_total: Decimal = Decimal(0)                # 归属于本标的成交的全部费用（买卖两侧）
    qty: Decimal = Decimal(0)                       # 期末持股（K + C）
    known_qty: Decimal = Decimal(0)                 # 其中有成本证据的数量（K）
    unknown_qty: Decimal = Decimal(0)               # 其中无成本证据的数量（C）
    avg_cost: Decimal | None = None                 # K 部分的平均成本；K=0 时 None
    cost_estimated: bool = False                    # 平均成本是否混有开账估算成本
    sells: int = 0
    buys: int = 0

    @property
    def realized_known(self) -> Decimal:
        return self.realized_exact + self.realized_estimated


@dataclass
class PnlResult:
    sells: list[SellPnl] = field(default_factory=list)
    by_code: dict[str, CodePnl] = field(default_factory=dict)
    pre_opening: list[PreOpeningTrade] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def totals_by_currency(self) -> dict[str, dict[str, Decimal]]:
        """逐币种汇总（币种之间不相加）。"""
        out: dict[str, dict[str, Decimal]] = {}
        for c in self.by_code.values():
            t = out.setdefault(c.currency, {"realized_exact": Decimal(0), "realized_estimated": Decimal(0),
                                            "unavailable_qty": Decimal(0), "fees_total": Decimal(0)})
            t["realized_exact"] += c.realized_exact
            t["realized_estimated"] += c.realized_estimated
            t["unavailable_qty"] += c.unavailable_qty
            t["fees_total"] += c.fees_total
        return out


class _State:
    def __init__(self, code: str, currency: str) -> None:
        self.code, self.currency = code, currency
        self.k_qty = Decimal(0)       # 有成本证据的股数
        self.k_cost = Decimal(0)      # 其总成本（含买入费用）
        self.est_qty = Decimal(0)     # K 中来自开账估算成本的股数
        self.c_qty = Decimal(0)       # 无成本证据的股数
        self.out = CodePnl(code, currency)

    @property
    def total(self) -> Decimal:
        return self.k_qty + self.c_qty

    def finalize(self) -> CodePnl:
        o = self.out
        o.qty, o.known_qty, o.unknown_qty = self.total, self.k_qty, self.c_qty
        o.avg_cost = (self.k_cost / self.k_qty) if self.k_qty > 0 else None
        o.cost_estimated = self.k_qty > 0 and self.est_qty > 0
        return o


def _fmt_qty(q: Decimal) -> str:
    return format(q.quantize(Decimal("0.0001")).normalize(), "f")


def _positive(name: str, v: Decimal) -> Decimal:
    v = dec(v)
    if v <= 0:
        raise PnlError(f"{name} 必须为正：{v}")
    return v


def _validate(e: TradeEvent) -> None:
    if e.kind not in KINDS:
        raise PnlError(f"未知事件类型：{e.kind!r}")
    if e.kind == SPLIT:
        if e.ratio_num <= 0 or e.ratio_den <= 0:
            raise PnlError("拆股比例必须为正")
        return
    _positive("qty", e.qty)
    if e.kind in (BUY, SELL):
        if e.price is None or dec(e.price) <= 0:
            raise PnlError(f"{e.kind} 必须带正的成交价")
    if dec(e.fee) < 0:
        raise PnlError("费用必须 ≥ 0（符号由 kind 决定）")


def compute_realized_pnl(events: Iterable[TradeEvent], opening_at: datetime | str | None = None) -> PnlResult:
    """按移动平均成本法计算已实现盈亏。

    events：任意顺序（内部按时间稳定排序；同一时刻先拆股、再期初、再成交）。
    opening_at：开账时点 t0；`at ≤ t0` 的 BUY/SELL 记为 pre_opening，不进入成本池；`at ≤ t0` 的拆股被忽略
    （期初持仓已是拆股后的数量）。None 表示没有开账点（所有成交都参与，同时给出告警）。
    """
    res = PnlResult()
    t0 = ensure_utc(opening_at) if opening_at is not None else None
    if t0 is None:
        res.warnings.append("no_opening")
    items = []
    for i, e in enumerate(events):
        _validate(e)
        items.append((ensure_utc(e.at), _RANK[e.kind], i, e))
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    states: dict[str, _State] = {}

    with localcontext() as ctx:
        ctx.prec = 40
        for at, _, _, e in items:
            if e.kind == SPLIT:
                if t0 is not None and at <= t0:
                    continue
                st = states.get(e.code)
                if st is None:
                    continue
                r = Decimal(e.ratio_num) / Decimal(e.ratio_den)
                st.k_qty, st.est_qty, st.c_qty = st.k_qty * r, st.est_qty * r, st.c_qty * r
                continue
            qty, fee = dec(e.qty), dec(e.fee)
            at_text = e.at if isinstance(e.at, str) else at.isoformat()
            if e.kind in (BUY, SELL) and t0 is not None and at <= t0:
                res.pre_opening.append(PreOpeningTrade(e.ref, e.code, e.currency, at_text, e.kind, qty, dec(e.price), fee))
                continue
            st = states.setdefault(e.code, _State(e.code, e.currency))
            if st.out.currency != e.currency:
                raise PnlError(f"{e.code} 出现多个币种：{st.out.currency} / {e.currency}")
            if e.kind == OPENING:
                if e.price is not None:
                    st.k_qty += qty
                    st.k_cost += qty * dec(e.price)
                    st.est_qty += qty
                else:
                    st.c_qty += qty
                continue
            px = dec(e.price)
            st.out.fees_total += fee
            if e.kind == BUY:
                st.k_qty += qty
                st.k_cost += qty * px + fee
                st.out.buys += 1
                continue
            _sell(st, res, e, at_text, qty, px, fee)

    for st in states.values():
        res.by_code[st.code] = st.finalize()
    return res


def _sell(st: _State, res: PnlResult, e: TradeEvent, at_text: str, qty: Decimal, px: Decimal, fee: Decimal) -> None:
    st.out.sells += 1
    total = st.total
    take = min(qty, total)
    excess = qty - take
    if excess > 0:
        res.warnings.append(f"oversold:{st.code}")
    if total <= 0:
        k_take = Decimal(0)
    elif take == total:
        k_take = st.k_qty
    else:
        k_take = take * st.k_qty / total
    c_take = take - k_take
    unavailable_qty = c_take + excess
    avg = (st.k_cost / st.k_qty) if st.k_qty > 0 else None
    fee_known = fee * k_take / qty
    realized: Decimal | None = None
    estimated = st.k_qty > 0 and st.est_qty > 0
    if k_take > 0 and avg is not None:
        realized = px * k_take - avg * k_take - fee_known
        if estimated:
            st.out.realized_estimated += realized
        else:
            st.out.realized_exact += realized
        # 按持股比例结转（平均成本不变）
        frac_left = (st.k_qty - k_take) / st.k_qty
        st.k_cost *= frac_left
        st.est_qty *= frac_left
        st.k_qty -= k_take
        if st.k_qty == 0:
            st.k_cost = Decimal(0)
            st.est_qty = Decimal(0)
    if c_take > 0:
        st.c_qty -= c_take
    if unavailable_qty > 0:
        st.out.unavailable_qty += unavailable_qty
        st.out.unavailable_net_proceeds += px * unavailable_qty - fee * unavailable_qty / qty
    if unavailable_qty == 0:
        quality = ESTIMATED if estimated else EXACT
    elif k_take > 0:
        quality = PARTIAL
    else:
        quality = UNAVAILABLE
    note = ""
    if excess > 0:
        note = f"超出可追溯库存 {_fmt_qty(excess)} 股（无成本证据）"
    elif c_take > 0:
        note = f"其中 {_fmt_qty(unavailable_qty)} 股来自无成本证据的开账持仓"
    res.sells.append(SellPnl(
        ref=e.ref, code=e.code, currency=e.currency, at=at_text, qty=qty, price=px, fee=fee,
        net_proceeds=px * qty - fee, avg_cost=avg if k_take > 0 else None, known_qty=k_take,
        unavailable_qty=unavailable_qty, realized=realized, quality=quality, note=note))


def trade_net_cashflow(events: Iterable[TradeEvent]) -> dict[str, Decimal]:
    """成交净现金流（逐币种）：买入为负（含费用）、卖出为正（扣费用）。**这不是盈亏**——
    买入尚未卖出的部分只是现金变成了持仓。函数名与口径刻意不含「盈亏/盈利」。"""
    out: dict[str, Decimal] = {}
    for e in events:
        if e.kind not in (BUY, SELL):
            continue
        amt = dec(e.qty) * dec(e.price) + dec(e.fee) if e.kind == BUY else -(dec(e.qty) * dec(e.price) - dec(e.fee))
        out[e.currency] = out.get(e.currency, Decimal(0)) - amt
    return out
