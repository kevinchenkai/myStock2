"""sleeve 引擎：一条线在一个币种袋内逐日模拟（ADR 0002 / exec-v1）。

- 每条线只读自己的状态；订单由 `provider(state, day)` 产生（M6 的 coach.decide 或测试脚本）。
- 同一套预算/撮合/结算/费用规则作用于所有正式模拟线（`human_plan`、`ai`、`ai_veto`、`buyhold`）。
- 缺行情不记零：持仓收盘价缺失或撮合结果不可知 → 当日 UNKNOWN，此后 PAUSED（不推进状态、不删日、不假定库存）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from mystock2.core import calendars as cal
from mystock2.ledger.fees import FeeRule, estimate, select_rule
from mystock2.ledger.settlement import SettlementRule, settle_date
from mystock2.scoreboard.matcher import match_order
from mystock2.scoreboard.types import BUY, SELL, DayResult, ExecProtocol, HBar, LineState, Lot, SimFill, SimOrder


class MarketData(Protocol):
    def hourly(self, code: str, day: date) -> list[HBar]: ...        # 仅完整 bar，按时间升序
    def session_complete(self, code: str, day: date) -> bool: ...    # bar 是否覆盖整个常规时段
    def close(self, code: str, day: date) -> Decimal | None: ...      # 未复权收盘价；缺失返回 None


Provider = Callable[[LineState, date], list[SimOrder]]


class EngineError(ValueError):
    pass


class ProviderUnknown(RuntimeError):
    """订单提供者无法确定当日应有的订单（如买入持有缺建仓行情）：该日 UNKNOWN，此后 PAUSED（不静默变成无订单）。"""


@dataclass
class LineRun:
    results: list[DayResult] = field(default_factory=list)
    start_states: dict[date, LineState] = field(default_factory=dict)   # 每日开盘前的状态（供 coach 与复算）
    final_state: LineState | None = None


def apply_splits(state: LineState, splits: dict[str, list[tuple[object, int, int]]], market: str, day: date) -> None:
    s = cal.session(market, day)
    prev = cal.prev_session(market, day)
    prev_close = cal.session(market, prev).close_utc
    for code, lots in state.lots.items():
        for eff, num, den in splits.get(code, []):
            if prev_close < eff <= s.open_utc:
                for lot in lots:
                    lot.qty = lot.qty * num / den
                    lot.unit_cost = lot.unit_cost * den / num


def _fee(rule: FeeRule, fills, protocol: ExecProtocol) -> tuple[Decimal, Decimal]:
    e = estimate(rule, fills)
    return e.fee * protocol.fee_multiplier, e.tax * protocol.fee_multiplier


def run_line(md: MarketData, *, market: str, currency: str, initial: LineState, sessions: list[date], provider: Provider,
             protocol: ExecProtocol, fee_rules: list[FeeRule], settlement: SettlementRule, lot_sizes: dict[str, int] | None = None,
             splits: dict[str, list[tuple[object, int, int]]] | None = None) -> LineRun:
    if settlement.lag_sessions is None:
        raise EngineError("结算规则未配置（lag_sessions=None）：失败关闭，不默认 T+0（ADR 0002）")
    lot_sizes = lot_sizes or {}
    splits = splits or {}
    state = initial.copy()
    run = LineRun()
    paused = False
    for day in sessions:
        if paused:
            run.results.append(DayResult(day, "PAUSED"))
            continue
        apply_splits(state, splits, market, day)
        state.unsettled = [(d, a) for d, a in state.unsettled if d > day]       # 结算日当天视为已结算
        run.start_states[day] = state.copy()
        try:
            orders = provider(state.copy(), day)
        except ProviderUnknown as exc:
            run.results.append(DayResult(day, "UNKNOWN", flags=[f"provider_unknown:{exc}"]))
            paused = True
            continue
        res, state, unknown = _simulate_day(md, market, day, state, orders, protocol, fee_rules, settlement, lot_sizes)
        run.results.append(res)
        if unknown:
            paused = True
    run.final_state = state
    return run


def _simulate_day(md, market, day, state: LineState, orders: list[SimOrder], protocol, fee_rules, settlement, lot_sizes):
    res = DayResult(day, "OK")
    st = state.copy()
    start_qty = {c: st.qty(c) for c in st.lots}                    # 开盘时的可卖库存（当日新买入当日不可卖）
    sold_so_far: dict[str, Decimal] = {}
    reserved = Decimal(0)
    accepted: list[tuple[SimOrder, list[HBar], bool, object]] = []
    tradable = st.tradable_cash()
    for o in orders:
        lot = lot_sizes.get(o.code, 1)
        if o.qty <= 0 or o.qty % lot != 0:
            res.rejected.append((o, "bad_lot"))
            continue
        bars, complete = md.hourly(o.code, day), md.session_complete(o.code, day)
        if o.side == SELL:
            avail = start_qty.get(o.code, Decimal(0)) - sold_so_far.get(o.code, Decimal(0))
            if Decimal(o.qty) > avail:
                res.rejected.append((o, "insufficient_inventory"))
                continue
            sold_so_far[o.code] = sold_so_far.get(o.code, Decimal(0)) + o.qty
            accepted.append((o, bars, complete, None))
        else:
            px = o.limit_price if o.limit_price is not None else (bars[0].open if bars else None)
            if px is None:
                res.rejected.append((o, "no_price"))
                continue
            px = px * (1 + protocol.slippage_bps / Decimal(10000))          # 按最坏成交价（含滑点）预留
            rule = select_rule(fee_rules, market, BUY, day.isoformat())
            fee_res, tax_res = _fee(rule, [(Decimal(o.qty), px)], protocol)
            if rule.basis == "fill":                                          # 逐笔计费：每个可能的成交 bar 都可能再收最低费与固定费
                fee_res += (rule.min_fee + rule.flat_fee) * max(0, len(bars) - 1) * protocol.fee_multiplier
            need = Decimal(o.qty) * px + fee_res + tax_res
            if need > tradable - reserved:
                res.rejected.append((o, "cash_insufficient"))
                continue
            reserved += need
            accepted.append((o, bars, complete, rule))
    # 撮合
    matches = [(o, match_order(o, bars, protocol, lot_size=lot_sizes.get(o.code, 1), complete=complete)) for o, bars, complete, _ in accepted]
    # 同一线同一标的买卖在同一 bar 都触价：不假设先后，标歧义
    ambiguous_bars: dict[tuple[str, object], bool] = {}
    for o1, m1 in matches:
        for o2, m2 in matches:
            if o1.code == o2.code and o1.side == BUY and o2.side == SELL:
                for b in set(m1.crossed_bars) & set(m2.crossed_bars):
                    ambiguous_bars[(o1.code, b)] = True
    unknown = any(m.status == "unknown" for _, m in matches)
    if unknown:
        res.status = "UNKNOWN"
        res.flags.append("unknown_fill_outcome")
        return res, state, True                                    # 状态不推进；该线进入 PAUSED
    seq = 0
    for o, m in matches:
        if not m.fills:
            continue
        rule = select_rule(fee_rules, market, o.side, day.isoformat())
        fee, tax = _fee(rule, [(Decimal(f.qty), f.price) for f in m.fills], protocol)
        total_qty = sum(f.qty for f in m.fills)
        notional = sum((Decimal(f.qty) * f.price for f in m.fills), Decimal(0))
        res.traded_notional += notional
        res.fees_day += fee + tax
        st.fees_cum += fee + tax
        if o.side == BUY:
            st.cash -= notional + fee + tax                                    # 买入税（如有）与费用一并扣减并计入成本
            unit = (notional + fee + tax) / total_qty
            st.lots.setdefault(o.code, []).append(Lot(Decimal(total_qty), unit, day))
        else:
            net = notional - fee - tax
            st.cash += net
            st.unsettled.append((settle_date(settlement, day), net))
            left = Decimal(total_qty)
            lots = st.lots[o.code]
            while left > 0:
                head = lots[0]
                take = min(head.qty, left)
                head.qty -= take
                left -= take
                if head.qty == 0:
                    lots.pop(0)
            if not lots:
                del st.lots[o.code]
        for f in m.fills:
            seq += 1
            amb = ambiguous_bars.get((o.code, f.bar.start), False)
            res.fills.append(SimFill(day, seq, o.code, o.side, Decimal(f.qty), f.price, fee * Decimal(f.qty) / total_qty, f.bar.start, amb))
            if amb and "ambiguous_bar" not in res.flags:
                res.flags.append("ambiguous_bar")
    # 日终估值（未复权收盘价；缺失则 UNKNOWN，不记零）
    pv = Decimal(0)
    for code in list(st.lots):
        px = md.close(code, day)
        if px is None:
            res.status = "UNKNOWN"
            res.flags.append(f"missing_close:{code}")
            return DayResult(day, "UNKNOWN", flags=res.flags), state, True
        pv += st.qty(code) * px
    res.equity = st.cash + pv + st.other_equity
    res.cash, res.unsettled, res.position_value = st.cash, st.unsettled_total(), pv
    res.positions = {c: st.qty(c) for c in st.lots}
    return res, st, False
