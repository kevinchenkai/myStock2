from datetime import date, timedelta
from decimal import Decimal

import pytest

from mystock2.coach.decide import BUY, HOLD, SELL, SKIP, Prediction, StrategyParams, decide
from mystock2.instruments.security_rule import SecurityRule, parse_bands
from mystock2.instruments.universe import validate_universe
from mystock2.ledger.fees import FeeRule
from mystock2.scoreboard.types import LineState, Lot

D = Decimal
TARGET = date(2026, 3, 5)
FEES = [FeeRule("syn", "US", "ANY", "order", "USD", pct_fee=D("0.001"), min_fee=D("1"))]
RULE = SecurityRule("X", "2026-01-01", None, 1, parse_bands('[{"tick":"0.01"}]'), "合成", True)
P = StrategyParams(k=D("2"), q_buy=D("0.2"), q_sell=D("0.8"), min_gain=D("0.01"), max_hold_days=5, exit_q=D("0.3"), budget_slice=D("0.3"), allow_add=False)


def uni(*codes, **kw):
    items = [{"code": c, "tier": "trade", "max_weight": kw.get("w", "0.5"), "max_lots": kw.get("lots", 1000)} for c in codes]
    return validate_universe({"instruments": items}).entries


def pred(low, high, close=None, pid="p1"):
    return Prediction(pid, D(str(close if close is not None else (low + high) / 2)), D(str(low)), D(str(high)), 250)


def run(state, codes, preds, params=P, equity=10000, rules=None, **kw):
    return decide(state, market="US", target_session=TARGET, universe=uni(*codes, **kw), predictions=preds,
                  rules=rules or {c: RULE for c in codes}, params=params, fee_rules=FEES, trade_equity=D(equity))


def cash_state(cash):
    return LineState("USD", D(str(cash)))


# ---------------------------------------------------------------- 买入
def test_buy_ticket_fields_rounding_budget_and_reservation():
    (t,) = run(cash_state(10000), ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    assert t.action == BUY and t.limit_price == D("97.00")                          # 95 + 0.2×10 = 97
    # 预算＝min(0.3×10000=3000, 现金 10000, 0.5×10000)=3000 → 3000/97=30.9 → 30 股；预留＝30×97+费用
    assert t.qty == 30 and t.reserved_cash == D(30) * 97 + D("2.91") and "edge_ok" in t.reason_codes
    assert t.uncertainty["interval"] == "predicted_low_high_not_a_fill_probability"   # 不展示无依据的成交概率
    assert not any("prob" in k for k in t.uncertainty)                              # 没有任何「概率」字段


def test_edge_gate_skips_when_interval_is_narrower_than_round_trip_cost():
    (t,) = run(cash_state(10000), ["US.NVDA"], {"US.NVDA": pred(99.9, 100.1)})       # 宽度 0.2% < 2×往返成本
    assert t.action == SKIP and t.reason_codes == ("no_edge",)
    wide = run(cash_state(10000), ["US.NVDA"], {"US.NVDA": pred(90, 110)})
    assert wide[0].action == BUY


def test_t02_three_signals_one_cash_budget_priority_order():
    st = cash_state(1500)
    out = run(st, ["US.NVDA", "US.TSLA", "US.AMD"], {c: pred(95, 105) for c in ("US.NVDA", "US.TSLA", "US.AMD")}, params=StrategyParams(
        k=D("2"), q_buy=D("0.2"), q_sell=D("0.8"), max_hold_days=5, exit_q=D("0.3"), budget_slice=D("1"), allow_add=False), w="1")
    assert [t.action for t in out] == [BUY, SKIP, SKIP]                             # 按优先顺序：第一只占满预算，其余 SKIP
    assert [t.reason_codes[0] for t in out[1:]] == ["cash_insufficient"] * 2
    assert out[0].reserved_cash <= D(1500)


def test_caps_max_lots_and_max_weight():
    (t,) = run(cash_state(100000), ["US.NVDA"], {"US.NVDA": pred(95, 105)}, w="0.5", lots=7)
    assert t.qty == 7                                                              # max_lots=7（手=1 股）
    (t,) = run(cash_state(100000), ["US.NVDA"], {"US.NVDA": pred(95, 105)}, params=StrategyParams(k=D("2"), q_buy=D("0.2"), max_hold_days=5,
               budget_slice=D("1"), q_sell=D("0.8"), exit_q=D("0.3")), w="0.05", equity=10000)
    assert t.qty == 5 and t.limit_price * t.qty <= D("500")                         # max_weight=5% × 10000


def test_lot_size_rounding_and_cheap_budget():
    hk = SecurityRule("HK.00700", "2026-01-01", None, 100, parse_bands('[{"tick":"0.2"}]'), "合成", True)
    ents = validate_universe({"instruments": [{"code": "HK.00700", "tier": "trade", "max_weight": "1", "max_lots": 5}]}).entries
    fees = [FeeRule("syn", "HK", "ANY", "order", "HKD", pct_fee=D("0.0003"), min_fee=D("3"))]
    out = decide(cash_state(300000), market="HK", target_session=TARGET, universe=ents, predictions={"HK.00700": pred(400, 440)},
                 rules={"HK.00700": hk}, params=StrategyParams(k=D("2"), q_buy=D("0.25"), max_hold_days=5, budget_slice=D("1"), q_sell=D("0.8"), exit_q=D("0.3")),
                 fee_rules=fees, trade_equity=D(300000))
    t = out[0]
    assert t.action == BUY and t.qty % 100 == 0 and t.qty == 500 and t.limit_price == D("410.0")   # 400+0.25×40=410，tick 0.2 合法；5 手


# ---------------------------------------------------------------- 缺参数/缺依据 → 不生成可执行数量
@pytest.mark.parametrize("missing", ["k", "q_buy", "max_hold_days", "budget_slice"])
def test_missing_buy_params_means_no_executable_quantity(missing):
    kw = dict(k=D("2"), q_buy=D("0.2"), q_sell=D("0.8"), max_hold_days=5, exit_q=D("0.3"), budget_slice=D("0.3"))
    kw[missing] = None
    (t,) = run(cash_state(10000), ["US.NVDA"], {"US.NVDA": pred(90, 110)}, params=StrategyParams(**kw))
    assert t.action == SKIP and t.qty is None and f"params_missing:{missing}" in t.reason_codes


def test_universe_blockers_prediction_and_rule_unknown():
    ents = validate_universe({"instruments": [
        {"code": "US.NVDA", "tier": "trade", "max_weight": "0.5", "max_lots": 9, "pending_confirmation": True},
        {"code": "US.TSLA", "tier": "trade"},
        {"code": "US.AMD", "tier": "trade", "max_weight": "0.5", "max_lots": 9},
        {"code": "US.PDD", "tier": "trade", "max_weight": "0.5", "max_lots": 9},
        {"code": "US.META", "tier": "core"},
    ]}).entries
    out = decide(cash_state(10000), market="US", target_session=TARGET, universe=ents,
                 predictions={"US.NVDA": pred(90, 110), "US.TSLA": pred(90, 110), "US.AMD": None, "US.PDD": pred(90, 110)},
                 rules={"US.NVDA": RULE, "US.TSLA": RULE, "US.AMD": RULE, "US.PDD": None}, params=P, fee_rules=FEES, trade_equity=D(10000))
    got = {t.code: (t.action, t.reason_codes) for t in out}
    assert got["US.NVDA"] == (SKIP, ("pending_confirmation",))                      # D1：确认前不出可执行单
    assert got["US.TSLA"][0] == SKIP and "max_weight_missing" in got["US.TSLA"][1] and "max_lots_missing" in got["US.TSLA"][1]
    assert got["US.AMD"] == (SKIP, ("prediction_unavailable",))
    assert got["US.PDD"] == (SKIP, ("rule_unknown",))
    assert "US.META" not in got                                                     # core 不出操作单


# ---------------------------------------------------------------- 卖出 / 时间止损（T-26）
def holding(qty, cost, acquired, cash=0):
    st = LineState("USD", D(str(cash)))
    st.lots["US.NVDA"] = [Lot(D(str(qty)), D(str(cost)), acquired)]
    return st


def test_sell_target_with_min_gain_floor_and_unreachable_hold():
    st = holding(100, 100, TARGET - timedelta(days=2))
    (t,) = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    # q_sell: 95+0.8×10=103；floor=100×1.01=101 <103 → 103
    assert t.action == SELL and t.limit_price == D("103.00") and t.qty == 100 and t.reason_codes == ("sell_target",)
    st2 = holding(100, 110, TARGET - timedelta(days=2))                              # 成本 110 → 地板 111.1 > 预测高点 105
    (t,) = run(st2, ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    assert t.action == HOLD and t.reason_codes == ("floor_above_predicted_high",)


def test_t26_time_stop_overrides_min_gain_and_is_not_blocked_by_price_below_cost():
    st = holding(100, 120, TARGET - timedelta(days=9))                               # 持有超期，且价格远低于成本
    (t,) = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    assert t.action == SELL and t.reason_codes == ("time_stop",) and t.limit_price == D("98.00")   # 95+0.3×10；不受 min_gain 约束
    st_edge = holding(100, 120, TARGET - timedelta(days=5))                          # 恰好 5 天：未超过 → 走正常卖出逻辑
    assert run(st_edge, ["US.NVDA"], {"US.NVDA": pred(95, 105)})[0].action == HOLD
    no_exit = StrategyParams(k=D("2"), q_buy=D("0.2"), q_sell=D("0.8"), max_hold_days=5, budget_slice=D("0.3"))
    assert run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)}, params=no_exit)[0].reason_codes == ("params_missing:exit_q",)


def test_sell_only_whole_lots_and_missing_sell_param():
    hk = SecurityRule("US.NVDA", "2026-01-01", None, 100, parse_bands('[{"tick":"0.01"}]'), "合成", True)
    st = holding(150, 100, TARGET - timedelta(days=1))
    (t,) = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)}, rules={"US.NVDA": hk})
    assert t.action == SELL and t.qty == 100                                         # 零股不出单
    st = holding(50, 100, TARGET - timedelta(days=1))
    assert run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)}, rules={"US.NVDA": hk})[0].reason_codes == ("odd_lot_only",)
    no_sell = StrategyParams(k=D("2"), q_buy=D("0.2"), max_hold_days=5, exit_q=D("0.3"), budget_slice=D("0.3"))
    assert run(holding(100, 100, TARGET - timedelta(days=1)), ["US.NVDA"], {"US.NVDA": pred(95, 105)}, params=no_sell)[0].reason_codes == ("params_missing:q_sell",)


def test_allow_add_falls_through_to_buy_only_when_sell_unreachable():
    add = StrategyParams(k=D("2"), q_buy=D("0.2"), q_sell=D("0.8"), min_gain=D("0.01"), max_hold_days=5, exit_q=D("0.3"), budget_slice=D("0.3"), allow_add=True)
    st = holding(10, 110, TARGET - timedelta(days=2), cash=10000)                    # 卖出目标不可达
    (t,) = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)}, params=add)
    assert t.action == BUY
    st_ok = holding(10, 100, TARGET - timedelta(days=2), cash=10000)                 # 卖出可达：卖出优先
    assert run(st_ok, ["US.NVDA"], {"US.NVDA": pred(95, 105)}, params=add)[0].action == SELL


# ---------------------------------------------------------------- 纯函数性质
def test_decide_is_pure_deterministic_and_does_not_mutate_state():
    st = holding(10, 100, TARGET - timedelta(days=2), cash=5000)
    before = st.hash()
    a = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    b = run(st, ["US.NVDA"], {"US.NVDA": pred(95, 105)})
    assert a == b and st.hash() == before
