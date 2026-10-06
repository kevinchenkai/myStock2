"""决策层：`decide(状态, 预测, 证券规则, 配置) → 操作单草稿`（纯函数，无 I/O；实施方案 §3.3、WP6.2）。

首期策略：透明的库存策略。
- 边际门槛：`(Ĥ−L̂)/中间价 ≥ k × c_rt`（c_rt＝往返费用与滑点比率），否则 SKIP(no_edge)。
- 买单：限价＝L̂ + q_buy×(Ĥ−L̂)（再保守舍入到合法 tick）；数量受交易仓预算切片、可交易现金、`max_weight`、`max_lots` 约束，向下取整到手；共享预算按冻结的优先顺序依次预留。
- 卖单：仅对已持有的交易仓出单；限价＝L̂ + q_sell×(Ĥ−L̂)，若有 `min_gain` 则不低于「均价×(1+min_gain)」；该价高于预测高点则 HOLD。
- 时间止损**优先于 min_gain**：持有天数 > max_hold_days 时发出退出单（限价＝L̂ + exit_q×(Ĥ−L̂)），不受最低获利约束；未成交次日续发（`exit_unfilled='carry'`）。
- **缺关键参数则不生成可执行数量**（k、q_buy、max_hold_days、budget_slice 等）；缺预测/规则 → SKIP 并给出原因码；绝不「补一个默认值」。
- 不展示无依据的概率；不强制取区间端点；参数属冻结协议（`StrategyParams.version`），观察前冻结。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from mystock2.core.money import floor_to_lots
from mystock2.instruments.security_rule import RuleUnknown, SecurityRule
from mystock2.instruments.universe import UniverseEntry
from mystock2.ledger.fees import FeeRule, estimate, max_buy_cost, possible_fills_bound, select_rule

BUY, SELL, HOLD, SKIP = "BUY", "SELL", "HOLD", "SKIP"


class StateView(Protocol):
    """决策层只读取线内状态的这些接口（scoreboard.LineState 满足它）。"""

    def qty(self, code: str) -> Decimal: ...
    def avg_cost(self, code: str) -> Decimal | None: ...
    def holding_age_days(self, code: str, today: date) -> int | None: ...
    def tradable_cash(self) -> Decimal: ...


@dataclass(frozen=True)
class StrategyParams:
    version: str = "inv-policy-v1"
    k: Decimal | None = None
    q_buy: Decimal | None = None
    q_sell: Decimal | None = None
    min_gain: Decimal | None = None
    max_hold_days: int | None = None
    exit_q: Decimal | None = None
    exit_unfilled: str = "carry"            # carry（次日续发）| void（作废）
    budget_slice: Decimal | None = None     # 每标的每日买入预算占交易仓权益的比例
    allow_add: bool | None = None           # 已持有时是否允许加仓
    slippage_bps: Decimal = Decimal(0)

    def buy_blockers(self) -> list[str]:
        need = {"k": self.k, "q_buy": self.q_buy, "max_hold_days": self.max_hold_days, "budget_slice": self.budget_slice}
        return [f"params_missing:{k}" for k, v in need.items() if v is None]


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    close: Decimal                 # T 的原始收盘价
    low: Decimal                   # L̂（价位）
    high: Decimal                  # Ĥ
    n_train: int = 0
    note: str = ""


@dataclass(frozen=True)
class TicketDraft:
    code: str
    action: str
    limit_price: Decimal | None = None
    qty: int | None = None
    lot_size: int | None = None
    reserved_cash: Decimal | None = None
    reason_codes: tuple[str, ...] = ()
    invalidate_if: tuple[str, ...] = ("position_changed", "snapshot_stale", "past_deadline", "data_corrected")
    uncertainty: dict = field(default_factory=dict)
    model_ref: str | None = None


def _skip(code: str, *reasons: str, model_ref: str | None = None) -> TicketDraft:
    return TicketDraft(code, SKIP, reason_codes=tuple(reasons), model_ref=model_ref)


def _px(pred: Prediction, q: Decimal) -> Decimal:
    return pred.low + q * (pred.high - pred.low)


def _round_trip_ratio(rules: list[FeeRule], market: str, day: str, qty: int, px: Decimal, slippage_bps: Decimal) -> Decimal:
    buy = estimate(select_rule(rules, market, BUY, day), [(Decimal(qty), px)])
    sell = estimate(select_rule(rules, market, SELL, day), [(Decimal(qty), px)])
    notional = Decimal(qty) * px
    return (buy.total + sell.total) / notional + Decimal(2) * slippage_bps / Decimal(10000)


def decide(state: StateView, *, market: str, target_session: date, universe: list[UniverseEntry], predictions: dict[str, Prediction | None],
           rules: dict[str, SecurityRule | None], params: StrategyParams, fee_rules: list[FeeRule], trade_equity: Decimal,
           priority: list[str] | None = None, split_pending: frozenset[str] | set[str] = frozenset()) -> list[TicketDraft]:
    """每个交易仓标的产出一张操作单草稿（BUY/SELL/HOLD/SKIP），按优先顺序依次预留共享预算。

    `target_session` 是被预测的交易日；持有天数按「目标日 − 最早持仓日」计。
    `split_pending`：数据日收盘之后、目标日开盘之前生效拆股/并股的标的。开盘状态的持仓已按拆股调整，而预测价位仍按拆股前收盘价换算，
    两者口径不一，**不出可执行单**（SKIP `split_pending`，审核 P1-9）；不在这里换算价位。
    """
    entries = {e.code: e for e in universe if e.market == market and e.tier == "trade"}
    order = [c for c in (priority or [e.code for e in universe]) if c in entries]
    order += [c for c in entries if c not in order]
    reserved = Decimal(0)
    out: list[TicketDraft] = []
    day = target_session.isoformat()
    for code in order:
        e = entries[code]
        pred, rule = predictions.get(code), rules.get(code)
        if pred is None:
            out.append(_skip(code, "prediction_unavailable"))
            continue
        if code in split_pending:
            out.append(_skip(code, "split_pending", model_ref=pred.prediction_id))
            continue
        if rule is None:
            out.append(_skip(code, "rule_unknown", model_ref=pred.prediction_id))
            continue
        try:
            lot = rule.lot_size
            if lot is None:
                raise RuleUnknown("lot_size 未知")
            rule.tick_for(pred.close)
        except RuleUnknown:
            out.append(_skip(code, "rule_unknown", model_ref=pred.prediction_id))
            continue
        unc = {"n_train": pred.n_train, "interval": "predicted_low_high_not_a_fill_probability", "note": pred.note}
        held = state.qty(code)
        mid = (pred.low + pred.high) / 2

        # ---------------- 持仓：时间止损 / 正常卖出
        if held > 0:
            age = state.holding_age_days(code, target_session)
            if params.max_hold_days is not None and age is not None and age > params.max_hold_days:
                if params.exit_q is None:
                    out.append(_skip(code, "params_missing:exit_q", model_ref=pred.prediction_id))
                    continue
                lim = rule.legal_limit(_px(pred, params.exit_q), SELL)      # 时间止损：不受 min_gain 约束
                exit_qty = int(floor_to_lots(held, lot))                      # 只出整手：零股单会被撮合整单拒绝，到期退出就静默消失
                if exit_qty <= 0:
                    out.append(_skip(code, "time_stop", "odd_lot_only", model_ref=pred.prediction_id))
                else:
                    out.append(TicketDraft(code, SELL, lim, exit_qty, lot, None, ("time_stop",), uncertainty=unc, model_ref=pred.prediction_id))
                continue
            if params.q_sell is None:
                out.append(_skip(code, "params_missing:q_sell", model_ref=pred.prediction_id))
                continue
            lim_raw = _px(pred, params.q_sell)
            reasons = ["sell_target"]
            if params.min_gain is not None:
                floor = state.avg_cost(code) * (1 + params.min_gain)
                if floor > lim_raw:
                    lim_raw, reasons = floor, ["sell_target", "min_gain_floor"]
            sell_unreachable = lim_raw > pred.high
            if sell_unreachable and not params.allow_add:
                out.append(TicketDraft(code, HOLD, reason_codes=("floor_above_predicted_high",), uncertainty=unc, model_ref=pred.prediction_id))
                continue
            if not sell_unreachable:
                sell_qty = int(floor_to_lots(held, lot))
                if sell_qty <= 0:
                    out.append(_skip(code, "odd_lot_only", model_ref=pred.prediction_id))
                else:
                    out.append(TicketDraft(code, SELL, rule.legal_limit(lim_raw, SELL), sell_qty, lot, None, tuple(reasons), uncertainty=unc,
                                           model_ref=pred.prediction_id))
                continue
            # 卖出目标不可达且允许加仓（D4）：继续评估买入

        # ---------------- 空仓：买入
        blockers = list(e.blockers) + params.buy_blockers()
        if held > 0 and not params.allow_add:
            blockers.append("add_not_allowed")
        if blockers:
            out.append(_skip(code, *blockers, model_ref=pred.prediction_id))
            continue
        limit = rule.legal_limit(_px(pred, params.q_buy), BUY)
        if limit <= 0:
            out.append(_skip(code, "invalid_limit", model_ref=pred.prediction_id))
            continue
        exposure = held * pred.close                                  # 已有暴露按最近收盘估值；max_weight 约束的是总持仓价值，不是单次买入
        headroom = max(Decimal(0), e.max_weight * trade_equity - exposure)
        budget = min(params.budget_slice * trade_equity, state.tradable_cash() - reserved, headroom)
        cap_by_lots = max(Decimal(0), Decimal(e.max_lots) * lot - held)     # max_lots 是总持仓手数上限（含已有）
        qty = int(floor_to_lots(min(budget / limit, cap_by_lots), lot))
        buy_rule = select_rule(fee_rules, market, BUY, day)
        bars_bound = possible_fills_bound(market, target_session)

        def need_of(n: int, rule=buy_rule, px=limit, bound=bars_bound) -> Decimal:   # 与记分牌引擎预留同一口径（费用＋税＋滑点＋逐笔费上限，审核 P1-10）
            return max_buy_cost(rule, n, px, slippage_bps=params.slippage_bps, possible_fills=bound)
        while qty > 0:
            if need_of(qty) <= min(budget, state.tradable_cash() - reserved):
                break
            qty -= lot
        if qty <= 0:
            reason = "cash_insufficient" if state.tradable_cash() - reserved < limit * lot else "budget_too_small"
            out.append(_skip(code, reason, model_ref=pred.prediction_id))
            continue
        c_rt = _round_trip_ratio(fee_rules, market, day, qty, limit, params.slippage_bps)
        width = (pred.high - pred.low) / mid
        if width < params.k * c_rt:
            out.append(TicketDraft(code, SKIP, reason_codes=("no_edge",), uncertainty={**unc, "width": str(width), "c_rt": str(c_rt)}, model_ref=pred.prediction_id))
            continue
        need = need_of(qty)
        reserved += need
        out.append(TicketDraft(code, BUY, limit, qty, lot, need, ("edge_ok", "buy_target"), uncertainty={**unc, "width": str(width), "c_rt": str(c_rt)},
                               model_ref=pred.prediction_id))
    return out
