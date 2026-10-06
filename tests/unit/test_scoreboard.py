from datetime import timedelta
from decimal import Decimal

import pytest

from mystock2.ledger import opening
from mystock2.ledger.events import EventDraft, fee_key, fill_key, post_event
from mystock2.ledger.fees import FeeRule
from mystock2.ledger.settlement import SettlementRule
from mystock2.scoreboard.engine import EngineError, run_line
from mystock2.scoreboard.human_actual import human_actual_series
from mystock2.scoreboard.lines import (
    BatchTerminated,
    BuyHoldProvider,
    apply_external_flow,
    batch_initial_state,
    create_batch,
    save_run,
)
from mystock2.scoreboard.matcher import match_order
from mystock2.scoreboard.metrics import daily_returns, summarize
from mystock2.scoreboard.stats import block_bootstrap_ci, paired_diff, required_days
from mystock2.scoreboard.types import BUY, SELL, DayResult, ExecProtocol, LineState, SimOrder
from tests.unit.ledger_helpers import ACCT
from tests.unit.ledger_helpers import make_db as make_ledger_db
from tests.unit.scoreboard_helpers import CODE, CODE2, DAYS, PROTO, RULES, SETTLE1, FakeMD, state

D = Decimal
d1, d2, d3, d4 = DAYS[0], DAYS[1], DAYS[2], DAYS[3]


def run(md, st, provider, days=None, **kw):
    return run_line(md, market="US", currency="USD", initial=st, sessions=days or DAYS[:3], provider=provider, protocol=kw.pop("protocol", PROTO),
                    fee_rules=RULES, settlement=kw.pop("settlement", SETTLE1), **kw)


def once(order_by_day):
    def provider(st, day):
        return list(order_by_day.get(day, []))
    return provider


# ------------------------------------------------------------ 撮合（T-05）
def hb(md, day, rows):
    md.set_day(CODE, day, rows)
    return md.hourly(CODE, day)


def test_touch_does_not_fill_strict_cross_does(tmp_path):
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9.5, 10, 100000)])
    o = SimOrder("L", CODE, BUY, 100, D("9.5"))
    assert match_order(o, bars, PROTO).status == "unfilled"            # 触价（low==limit）不成交
    o2 = SimOrder("L", CODE, BUY, 100, D("9.6"))
    m = match_order(o2, bars, PROTO)
    assert m.status == "filled" and m.fills[0].price == D("9.6")


def test_gap_through_limit_still_fills_at_limit_not_better_open():
    md = FakeMD()
    bars = hb(md, d1, [(8, 9, 7.5, 8.5, 100000)])                       # 开盘 8 远低于买入限价 10：真实可能更优，协议不享受
    m = match_order(SimOrder("L", CODE, BUY, 100, D("10")), bars, PROTO)
    assert m.fills[0].price == D("10")
    sell_bars = hb(md, d2, [(12, 13, 11.5, 12.5, 100000)])               # 卖出同理：开盘 12 高于限价 10，仍按 10 成交
    assert match_order(SimOrder("L", CODE, SELL, 100, D("10")), sell_bars, PROTO).fills[0].price == D("10")
    assert match_order(SimOrder("L", CODE, SELL, 100, D("11")), sell_bars, PROTO).fills[0].price == D("11")


def test_participation_cap_partial_and_multi_bar_accumulation():
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9, 10, 1000), (10, 11, 9, 10, 1000), (10, 11, 9, 10, 1000)])   # 每 bar 容量 10%×1000=100
    m = match_order(SimOrder("L", CODE, BUY, 250, D("9.5")), bars, PROTO)
    assert [f.qty for f in m.fills] == [100, 100, 50] and m.status == "filled"
    m = match_order(SimOrder("L", CODE, BUY, 400, D("9.5")), bars, PROTO)
    assert sum(f.qty for f in m.fills) == 300 and m.remaining == 100 and m.status == "partial"
    assert match_order(SimOrder("L", CODE, BUY, 400, D("9.5")), bars, ExecProtocol(max_participation=D("0.5"))).status == "filled"


def test_incomplete_session_or_missing_volume_is_unknown_unless_fully_filled():
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9, 10, 1000)])
    assert match_order(SimOrder("L", CODE, BUY, 500, D("9.5")), bars, PROTO, complete=False).status == "unknown"   # 部分成交 + 数据不全
    assert match_order(SimOrder("L", CODE, BUY, 100, D("9.5")), bars, PROTO, complete=False).status == "filled"    # 已全部成交：可知
    assert match_order(SimOrder("L", CODE, BUY, 100, D("5")), bars, PROTO, complete=False).status == "unknown"     # 未成交但缺数据：可能已成交
    assert match_order(SimOrder("L", CODE, BUY, 100, D("5")), bars, PROTO, complete=True).status == "unfilled"     # 数据完整的未成交：确定
    nov = hb(md, d2, [(10, 11, 9, 10, None)])
    assert match_order(SimOrder("L", CODE, BUY, 100, D("9.5")), nov, PROTO).status == "unknown"


def test_slippage_sensitivity_and_lot_validation():
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9, 10, 100000)])
    m = match_order(SimOrder("L", CODE, BUY, 100, D("10")), bars, ExecProtocol(slippage_bps=D("100")))
    assert m.fills[0].price == D("10.1")                                 # 对买方不利
    with pytest.raises(ValueError):
        match_order(SimOrder("L", CODE, BUY, 150, D("10")), bars, PROTO, lot_size=100)


# ------------------------------------------------------------ 引擎
def test_buy_hold_cash_fees_and_equity_arithmetic():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.set_day(CODE, d1, [(10, 11, 9, 10, 1_000_000)] * 6, close=10)
    r = run(md, state(10000), once({d1: [SimOrder("L", CODE, BUY, 100, D("9.5"))]}), days=[d1, d2])
    a, b = r.results
    fee = D("1.00")                                                       # 100×9.5=950×0.001=0.95 → 最低费 1
    assert a.status == "OK" and a.cash == D("10000") - 950 - fee and a.positions == {CODE: D(100)}
    assert a.equity == a.cash + 100 * 10 and a.fees_day == fee
    assert b.equity == a.equity                                           # 价格不变，权益不变
    assert r.final_state.avg_cost(CODE) == (D(950) + fee) / 100           # 成本含费用


def test_cash_reserve_rejects_and_inventory_rejects():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    r = run(md, state(500), once({d1: [SimOrder("L", CODE, BUY, 100, D("10")),            # 需要 1000+费 > 500
                                      SimOrder("L", CODE, SELL, 10, D("11"))]}), days=[d1])
    assert [(o.side, why) for o, why in r.results[0].rejected] == [(BUY, "cash_insufficient"), (SELL, "insufficient_inventory")]
    assert r.results[0].cash == D(500) and r.results[0].fills == []


def test_two_buys_share_one_cash_budget_in_order():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.flat(CODE2, DAYS, 10)
    r = run(md, state(1100), once({d1: [SimOrder("L", CODE, BUY, 100, D("10")), SimOrder("L", CODE2, BUY, 100, D("10"))]}), days=[d1])
    assert [(o.code, why) for o, why in r.results[0].rejected] == [(CODE2, "cash_insufficient")]    # 预留后第二单不足（T-02 的引擎侧）


def test_same_day_buy_cannot_be_sold_same_day():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    r = run(md, state(10000), once({d1: [SimOrder("L", CODE, BUY, 100, D("10.5")), SimOrder("L", CODE, SELL, 100, D("9.5"))]}), days=[d1])
    assert [why for _, why in r.results[0].rejected] == ["insufficient_inventory"]    # 当日新买入当日不可卖


def test_t23_sale_proceeds_unsettled_then_released_next_session():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.flat(CODE2, DAYS, 10)
    st = state(0, US_NVDA=(100, 8, d1 - timedelta(days=5)))
    prov = once({d1: [SimOrder("L", CODE, SELL, 100, D("9.5"))],
                 d2: [SimOrder("L", CODE2, BUY, 90, D("10"))],      # d2：回款 settle_date=d2（lag 1）→ 当天已可用（949 ≥ 900+1）
                 })
    r = run(md, st, prov, days=[d1, d2])
    d1r, d2r = r.results
    assert d1r.unsettled and d1r.cash > 0 and d1r.equity == d1r.cash                    # 权益含未结算回款
    assert d2r.rejected == [] and d2r.positions == {CODE2: D(90)}
    # 当日（d1）想用回款买入：被拒
    prov2 = once({d1: [SimOrder("L", CODE, SELL, 100, D("9.5")), SimOrder("L", CODE2, BUY, 100, D("10"))]})
    r2 = run(md, state(0, US_NVDA=(100, 8, d1 - timedelta(days=5))), prov2, days=[d1])
    assert [why for _, why in r2.results[0].rejected] == ["cash_insufficient"]


def test_settlement_must_be_configured_and_fee_rule_must_exist():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    with pytest.raises(EngineError, match="结算规则未配置"):
        run(md, state(1000), once({}), settlement=SettlementRule("US", None))
    with pytest.raises(LookupError):                                                     # 无费用档案：不默认为 0
        run_line(md, market="US", currency="USD", initial=state(10000), sessions=[d1], provider=once({d1: [SimOrder("L", CODE, BUY, 10, D("10"))]}),
                 protocol=PROTO, fee_rules=[], settlement=SETTLE1)


def test_ambiguous_double_sided_bar_is_flagged_and_sell_uses_opening_inventory_only():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.set_day(CODE, d1, [(10, 12, 8, 10, 1_000_000)] * 6, close=10)          # 单 bar 同时穿越买 9 与卖 11
    st = state(5000, US_NVDA=(100, 9, d1 - timedelta(days=3)))
    r = run(md, st, once({d1: [SimOrder("L", CODE, BUY, 100, D("9")), SimOrder("L", CODE, SELL, 100, D("11"))]}), days=[d1])
    res = r.results[0]
    assert "ambiguous_bar" in res.flags and all(f.ambiguous for f in res.fills)
    assert res.positions == {CODE: D(100)}                                     # 卖掉开盘的 100、买入 100：不假设「先买后卖」多卖
    assert sorted(f.side for f in res.fills) == [BUY, SELL]


def test_t31_determinate_failure_continues_but_missing_data_pauses():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    order_day2 = once({d2: [SimOrder("L", CODE, BUY, 100, D("1"))]})            # 价格远低，行情完整 → 确定未成交
    r = run(md, state(5000), order_day2, days=[d1, d2, d3])
    assert [x.status for x in r.results] == ["OK", "OK", "OK"] and r.results[1].fills == []
    # 缺 bar（不完整）且订单未成交：不可知 → UNKNOWN，其后 PAUSED；状态不推进、不删日
    md.complete[(CODE, d2)] = False
    r = run(md, state(5000), order_day2, days=[d1, d2, d3, d4])
    assert [x.status for x in r.results] == ["OK", "UNKNOWN", "PAUSED", "PAUSED"]
    assert r.results[1].equity is None and "unknown_fill_outcome" in r.results[1].flags      # 不记零
    assert r.final_state.cash == D(5000)                                                      # 状态未推进


def test_missing_close_for_held_position_is_unknown_not_zero():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.closes[(CODE, d2)] = None
    r = run(md, state(1000, US_NVDA=(10, 9, d1 - timedelta(days=3))), once({}), days=[d1, d2, d3])
    assert [x.status for x in r.results] == ["OK", "UNKNOWN", "PAUSED"] and "missing_close:US.NVDA" in r.results[1].flags


def test_split_adjusts_quantity_and_cost_on_effective_day():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    from datetime import datetime, timezone
    eff = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)                       # d2 开盘前
    st = state(0, US_NVDA=(100, 20, d1 - timedelta(days=3)))
    r = run(md, st, once({}), days=[d1, d2], splits={CODE: [(eff, 2, 1)]})
    assert r.results[1].positions == {CODE: D(200)} and r.final_state.lots[CODE][0].unit_cost == D(10)


def test_t25_each_line_reads_only_its_own_state():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    def rule_sell_if_held(st, day):          # 同一个规则：持有就卖
        return [SimOrder("L", CODE, SELL, int(st.qty(CODE)), D("9.5"))] if st.qty(CODE) > 0 else []
    ai = run(md, state(1000, US_NVDA=(50, 9, d1 - timedelta(days=2))), rule_sell_if_held, days=[d1])        # AI 昨日已持有
    human = run(md, state(1000), rule_sell_if_held, days=[d1])                                              # 人类线没有库存
    assert len(ai.results[0].fills) == 1 and human.results[0].fills == []
    assert ai.start_states[d1].hash() != human.start_states[d1].hash()


# ------------------------------------------------------------ 买入持有（T-41）
def test_buyhold_buys_once_at_first_open_keeps_leftover_and_never_trades():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.set_day(CODE, d1, [(10.0, 10.5, 9.5, 10.2, 1_000_000)] * 6, close=10.2)
    prov = BuyHoldProvider(md, market="US", weights={CODE: D("0.5")}, first_day=d1, lot_sizes={CODE: 1}, fee_rules=RULES, protocol=PROTO)
    r = run(md, state(10000), prov, days=[d1, d2, d3])
    f = r.results[0].fills
    assert len(f) == 1 and f[0].price == D("10.0") and f[0].qty == D(499)     # 5000/10=500 股，扣费后取 499 股（不倒填成交价）
    assert r.results[1].fills == [] and r.results[2].fills == []
    assert r.results[0].cash == D(10000) - D(499) * 10 - D("4.99") and r.results[0].cash > 5000     # 余款留现金
    assert prov(state(0), d2) == []                                             # 此后永不交易
    with pytest.raises(ValueError):
        BuyHoldProvider(md, market="US", weights={CODE: D("0.7"), CODE2: D("0.5")}, first_day=d1, lot_sizes={}, fee_rules=RULES, protocol=PROTO)


# ------------------------------------------------------------ 指标 / 统计
def mk(statuses_equities, start=DAYS[0]):
    return [DayResult(DAYS[i], s, e) for i, (s, e) in enumerate(statuses_equities)]


def test_returns_drawdown_coverage_and_unknown_days_are_not_zero():
    e0 = D(1000)
    res = mk([("OK", D(1010)), ("OK", D(1100)), ("UNKNOWN", None), ("PAUSED", None), ("OK", D(990))])
    r = daily_returns(res, e0)
    assert r[DAYS[0]] == D("0.01") and r[DAYS[1]] == D("0.09")
    assert DAYS[2] not in r and DAYS[3] not in r and DAYS[4] not in r        # 缺失之后的第一天没有前值：不产生（也不记零）收益
    s = summarize(res, e0)
    assert s.cumulative_return == D("-0.01") and s.max_drawdown == (D(1100) - D(990)) / D(1100)
    assert (s.days_ok, s.unknown_days, s.paused_days) == (3, 1, 1) and s.coverage == D("0.6")
    assert summarize([], e0).coverage is None and summarize([], e0).cumulative_return is None


def test_t33_cumulative_difference_equals_sum_of_daily_differences():
    e0 = D(2100)                                                               # E0=E_trade(t0)=1000+1000+100（含应收）
    a = mk([("OK", D(2100) + 21 * (i + 1) ** 1) for i in range(5)])
    b = mk([("OK", D(2100) + 7 * (i + 1) + (3 if i % 2 else -2)) for i in range(5)])
    assert summarize(mk([("OK", D(2100))]), e0).cumulative_return == 0          # 开账日 R=0
    pd_ = paired_diff(a, b, e0)
    ra, rb = summarize(a, e0).cumulative_return, summarize(b, e0).cumulative_return
    assert pd_.cumulative == ra - rb and pd_.n == 5 and pd_.coverage == 1


def test_paired_diff_uses_only_days_both_known_and_reports_coverage():
    e0 = D(1000)
    a = mk([("OK", D(1010)), ("OK", D(1020)), ("OK", D(1030)), ("OK", D(1040))])
    b = mk([("OK", D(1000)), ("UNKNOWN", None), ("PAUSED", None), ("PAUSED", None)])
    p = paired_diff(a, b, e0)
    assert list(p.deltas) == [DAYS[0]] and p.coverage == D("0.25") and p.n == 1


def test_block_bootstrap_is_deterministic_descriptive_and_refuses_tiny_samples():
    vals = [0.001 * ((i % 7) - 3) + 0.0002 for i in range(120)]
    a = block_bootstrap_ci(vals, block_len=5, n_boot=500, seed=1)
    assert a == block_bootstrap_ci(vals, block_len=5, n_boot=500, seed=1) and a["lo"] < a["mean"] < a["hi"] and "描述" in a["note"]
    tiny = block_bootstrap_ci([0.1, 0.2, 0.3], block_len=5)
    assert tiny["lo"] is None and tiny["hi"] is None


def test_required_days_matches_plan_power_table():
    assert round(required_days(0.0005, 0.01)) == 3136      # §2.3：≈3,100
    assert round(required_days(0.0010, 0.01)) == 784
    assert round(required_days(0.0020, 0.01)) == 196


# ------------------------------------------------------------ 批次与持久化（T-29、T-43）
def test_batch_state_package_and_runs_are_immutable_and_additive(tmp_path):
    from mystock2.core import db as dbmod
    p = tmp_path / "s.db"
    dbmod.migrate(p)
    conn = dbmod.connect_writer(p, "scoreboard")
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    init = state(5000, US_NVDA=(100, 9, d1 - timedelta(days=3)))
    create_batch(conn, "B1", PROTO, d1, init, D(5900), ["ai", "human_plan", "buyhold"])
    with pytest.raises(ValueError, match="修改＝新批次"):
        create_batch(conn, "B1", PROTO, d1, init, D(5900), ["ai"])
    with pytest.raises(ValueError):
        create_batch(conn, "B2", PROTO, d1, init, D(5900), ["oracle"])
    assert batch_initial_state(conn, "B1").hash() == init.hash()               # 每条线从同一完整状态包起跑
    runs = {f"B1:{k}": run(md, batch_initial_state(conn, "B1"), once({}), days=[d1, d2]) for k in ("ai", "human_plan")}
    save_run(conn, "run-1", "B1", PROTO, ["snap-a"], runs, D(5900), "USD")
    assert conn.execute("SELECT COUNT(*) c FROM sleeve_daily").fetchone()["c"] == 4
    assert conn.execute("SELECT metrics_json FROM eval_run").fetchone()["metrics_json"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        save_run(conn, "run-1", "B1", PROTO, [], runs, D(5900), "USD")           # 不覆盖
    for sql in ("UPDATE sleeve_daily SET equity='0'", "DELETE FROM sleeve_daily", "UPDATE comparison_batch SET e0='1'", "DELETE FROM eval_run"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    # 新候选 = 新批次：同一起点复制同一状态包
    create_batch(conn, "B2", PROTO, d1, init, D(5900), ["ai", "ai_lgbm"])
    assert batch_initial_state(conn, "B2").hash() == batch_initial_state(conn, "B1").hash()


def test_t43_external_flow_into_a_formal_line_terminates_batch():
    with pytest.raises(BatchTerminated):
        apply_external_flow(state(1000), D(500))


# ------------------------------------------------------------ human_actual（描述性）
def test_human_actual_books_real_fills_as_is_with_negative_cash_allowed(tmp_path):
    conn = make_ledger_db(tmp_path)
    t0 = "2026-02-27T00:00:00.000000Z"
    opening.record_opening(conn, ACCT, t0, {CODE: "10"}, {"USD": "100000"})
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.flat(CODE2, DAYS, 20)
    md.set_day(CODE, d2, [(10, 12, 9, 12, 1_000_000)] * 6, close=12)
    md.set_day(CODE2, d2, [(20, 21, 19, 21, 1_000_000)] * 6, close=21)
    t = "2026-03-03T16:00:00.000000Z"
    post_event(conn, EventDraft(fill_key(ACCT, "H1"), ACCT, "FILL", t, "USD", code=CODE, price="11", qty_delta="100", cash_delta="-1100", ref_deal_id="H1"))
    post_event(conn, EventDraft(fee_key(ACCT, "H1", "commission"), ACCT, "FEE", t, "USD", cash_delta="-1.5", ref_deal_id="H1"))
    post_event(conn, EventDraft("dep:1", ACCT, "DEPOSIT", t, "USD", cash_delta="9999"))                         # 入金不进入 human_actual（T-43）
    post_event(conn, EventDraft(fill_key(ACCT, "H2"), ACCT, "FILL", t, "USD", code="US.AAPL", price="5", qty_delta="1", cash_delta="-5", ref_deal_id="H2"))   # 名单外
    res = human_actual_series(conn, md, account_id=ACCT, market="US", currency="USD", codes={CODE, CODE2}, budget=D(500), d0=DAYS[0], sessions=[d1, d2, d3])
    a, b, c = res
    assert a.equity == D(500) + 10 * 10                                         # d1：预算现金 + 开账持仓（真实库存里属于名单的部分）
    assert b.cash == D(500) - 1100 - D("1.5") and b.cash < 0                    # 现金可为负；真实成交原样入账；费用取实际
    assert b.positions == {CODE: D(110)} and b.equity == b.cash + 110 * 12
    assert b.fills and b.fills[0].price == D(11) and "descriptive_actual" in b.flags
    assert c.cash == b.cash and c.equity == c.cash + 110 * 10                   # 入金与名单外成交均不影响
    md.closes[(CODE, d3)] = None
    assert human_actual_series(conn, md, account_id=ACCT, market="US", currency="USD", codes={CODE}, budget=D(0), d0=DAYS[0], sessions=[d3])[0].status == "UNKNOWN"


def test_sensitivity_variants_are_pessimistic_and_exclusion_removes_ambiguous_days():
    from mystock2.scoreboard.stats import ambiguous_dates
    from mystock2.scoreboard.types import sensitivity_variants
    v = sensitivity_variants(PROTO)
    assert v["fee_x2"].fee_multiplier == 2 and v["slippage_10bps"].slippage_bps == 10 and v["participation_half"].max_participation == D("0.05")
    assert all(x.version == PROTO.version for x in v.values())
    e0 = D(1000)
    a = mk([("OK", D(1010)), ("OK", D(1030)), ("OK", D(1040))])
    b = mk([("OK", D(1000)), ("OK", D(1010)), ("OK", D(1020))])
    a[1].flags.append("ambiguous_bar")
    full, trimmed = paired_diff(a, b, e0), paired_diff(a, b, e0, exclude_dates=ambiguous_dates(a, b))
    assert full.n == 3 and trimmed.n == 2 and DAYS[1] not in trimmed.deltas and trimmed.coverage == D(2) / 3


# ---------------------------------------------------------------- 代码评审回归（F09–F13、F19）
def test_f13_unknown_early_volume_is_not_masked_by_later_full_fill():
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9, 10, None), (10, 11, 9, 10, 1000), (10, 11, 9, 10, 1000)])      # 第一根穿越但容量未知
    assert match_order(SimOrder("L", CODE, BUY, 100, D("9.5")), bars, ExecProtocol(max_participation=D("0.5"))).status == "unknown"


def test_f19_valid_until_limits_matching_to_earlier_bars():
    md = FakeMD()
    bars = hb(md, d1, [(10, 11, 9, 10, 100000), (10, 11, 9, 10, 100000), (10, 11, 9, 10, 100000)])
    o = SimOrder("L", CODE, BUY, 100, D("9.5"), valid_until=bars[1].start)                         # 只有第一根 bar 在有效期内
    m = match_order(o, bars, PROTO)
    assert [f.bar.start for f in m.fills] == [bars[0].start]
    late = SimOrder("L", CODE, BUY, 100, D("9.5"), valid_until=bars[0].start)                      # 有效期在开盘之前：全部不可成交
    assert match_order(late, bars, PROTO).status == "unfilled"


def test_f11_reserve_covers_slippage_fill_level_flat_fees_and_buy_tax():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    rules = [FeeRule("syn", "US", "ANY", "fill", "USD", pct_fee=D("0"), min_fee=D("0"), flat_fee=D("5"), tax_pct=D("0.01"))]
    # 现金 100：买 2 股 @10（20）+ 税 0.2 + 单笔固定费 5，逐笔计费最多 6 个 bar → 保守预留 20+0.2+5+5×5=50.2 ≤ 100：被接受；现金不会为负
    r = run_line(md, market="US", currency="USD", initial=state(100), sessions=[d1], provider=once({d1: [SimOrder("L", CODE, BUY, 2, D("10.5"))]}),
                 protocol=PROTO, fee_rules=rules, settlement=SETTLE1)
    res = r.results[0]
    assert res.cash is not None and res.cash >= 0 and res.fills                                                    # 买入税与费用都扣了，且不透支
    assert res.cash == D(100) - D("21") - D("0.21") - D(5)                                                         # 2×10.5=21、税 0.21、一笔固定费 5
    # 同一订单预留不足 → 整单拒绝而不是透支：现金 30 不够 20+税+固定费×6
    r = run_line(md, market="US", currency="USD", initial=state(30), sessions=[d1], provider=once({d1: [SimOrder("L", CODE, BUY, 2, D("10.5"))]}),
                 protocol=PROTO, fee_rules=rules, settlement=SETTLE1)
    assert [why for _, why in r.results[0].rejected] == ["cash_insufficient"] and r.results[0].cash == D(30)
    slip = ExecProtocol(slippage_bps=D("1000"))                                                                    # 10% 滑点：预留按最坏成交价
    r = run_line(md, market="US", currency="USD", initial=state(22), sessions=[d1], provider=once({d1: [SimOrder("L", CODE, BUY, 2, D("10.5"))]}),
                 protocol=slip, fee_rules=[FeeRule("syn", "US", "ANY", "order", "USD")], settlement=SETTLE1)
    assert [why for _, why in r.results[0].rejected] == ["cash_insufficient"]                                      # 2×10.5×1.1=23.1 > 22


def test_f12_buyhold_missing_first_bar_is_unknown_and_pauses_not_idle_cash():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    md.bars[(CODE, d1)] = []                                                                                       # 建仓日缺小时线
    prov = BuyHoldProvider(md, market="US", weights={CODE: D("0.5")}, first_day=d1, lot_sizes={CODE: 1}, fee_rules=RULES, protocol=PROTO)
    r = run(md, state(10000), prov, days=[d1, d2, d3])
    assert [x.status for x in r.results] == ["UNKNOWN", "PAUSED", "PAUSED"] and r.results[0].flags[0].startswith("provider_unknown")
    late_open = FakeMD()
    late_open.flat(CODE, DAYS, 10)
    s0 = late_open.hourly(CODE, d1)
    late_open.bars[(CODE, d1)] = s0[2:]                                                                            # 缺开盘的前两根：不得拿后面的 bar 当「开盘价」
    prov2 = BuyHoldProvider(late_open, market="US", weights={CODE: D("0.5")}, first_day=d1, lot_sizes={CODE: 1}, fee_rules=RULES, protocol=PROTO)
    assert run(late_open, state(10000), prov2, days=[d1]).results[0].status == "UNKNOWN"


def test_f09_other_equity_enters_equity_and_e0_and_state_hash():
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    st = state(1000, US_NVDA=(100, 8, d1 - timedelta(days=30)))
    st.other_equity = D(100)                                                                                       # 期初应收 100
    r = run(md, st, once({}), days=[d1])
    e0 = D(1000) + 100 * 10 + 100                                                                                  # E0=B+库存+应收 = 2100
    assert r.results[0].equity == e0 and summarize(r.results, e0).cumulative_return == 0                           # 开账 R=0（漏掉应收则会是 5%）
    other = state(1000, US_NVDA=(100, 8, d1 - timedelta(days=30)))
    assert other.hash() != st.hash()
    assert LineState.from_dict(st.to_dict()).other_equity == D(100) and LineState.from_dict(st.to_dict()).hash() == st.hash()


def test_f25_human_actual_applies_splits_via_ledger_projection(tmp_path):
    from mystock2.ledger.opening import add_split
    conn = make_ledger_db(tmp_path)
    opening.record_opening(conn, ACCT, "2026-02-27T00:00:00.000000Z", {CODE: "10"}, {"USD": "100000"})
    add_split(conn, CODE, "2026-03-04T12:00:00Z", 2, 1)                                  # d2（03-04）开盘前 2:1 拆股
    md = FakeMD()
    for d in DAYS:
        md.set_day(CODE, d, [(10, 11, 9, 10, 1_000_000)] * 6, close=10 if d >= DAYS[2] else 20)
    res = human_actual_series(conn, md, account_id=ACCT, market="US", currency="USD", codes={CODE}, budget=D(0), d0=DAYS[0], sessions=[DAYS[1], DAYS[2]])
    before, after = res
    assert before.equity == D(10) * 20 and after.equity == D(20) * 10 and after.positions == {CODE: D(20)}     # 价格减半、数量翻倍：权益不变


def test_p1_10_sizing_and_engine_reservation_share_one_cost_function():
    """审核 P1-10：出单方定量（教练、买入持有、人类计划）与引擎预留必须同口径（含买入税、逐笔计费上限）；
    否则「出单方认为可行」的单被引擎整单以现金不足拒绝，买入持有还会静默空仓。"""
    from mystock2.coach.decide import Prediction, StrategyParams, decide
    from mystock2.instruments.security_rule import SecurityRule, parse_bands
    from mystock2.instruments.universe import validate_universe

    taxed = [FeeRule("syn", "US", "ANY", "order", "USD", pct_fee=Decimal("0"), min_fee=Decimal("3"), tax_pct=Decimal("0.001"))]
    per_fill = [FeeRule("syn", "US", "ANY", "fill", "USD", pct_fee=Decimal("0"), min_fee=Decimal("1"))]
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    settle = SettlementRule("US", 1)
    # 买入持有：预算 10005、整手 100、价 10：含 0.1% 税后 1000 股要 10013 → 只能买 900 股，不能空仓
    prov = BuyHoldProvider(md, market="US", weights={CODE: Decimal(1)}, first_day=DAYS[0], lot_sizes={CODE: 100}, fee_rules=taxed, protocol=PROTO)
    r = run_line(md, market="US", currency="USD", initial=LineState("USD", Decimal("10005")), sessions=DAYS[:2], provider=prov, protocol=PROTO,
                 fee_rules=taxed, settlement=settle, lot_sizes={CODE: 100})
    assert not r.results[0].rejected and r.results[-1].positions.get(CODE) == Decimal(900)
    uni = validate_universe({"instruments": [{"code": CODE, "tier": "trade", "max_weight": "1", "max_lots": 1000}]}).entries
    params = StrategyParams(k=Decimal("0.1"), q_buy=Decimal("0.5"), q_sell=Decimal("0.8"), max_hold_days=5, exit_q=Decimal("0.3"),
                            budget_slice=Decimal("1"), allow_add=False)
    pred = {CODE: Prediction("p", Decimal(10), Decimal("9.5"), Decimal("10.5"), 250)}
    for cash, fees, lot in ((Decimal("10005"), taxed, 100), (Decimal("993"), per_fill, 1)):
        rule = SecurityRule(CODE, "2026-01-01", None, lot, parse_bands('[{"tick":"0.01"}]'), "合成", True)
        (t,) = decide(LineState("USD", cash), market="US", target_session=DAYS[0], universe=uni, predictions=pred, rules={CODE: rule}, params=params,
                      fee_rules=fees, trade_equity=cash)
        assert t.action == BUY and t.reserved_cash <= cash
        x = run_line(md, market="US", currency="USD", initial=LineState("USD", cash), sessions=DAYS[:1],
                     provider=lambda s, d, t=t: [SimOrder("ai", CODE, t.action, t.qty, t.limit_price)], protocol=PROTO, fee_rules=fees,
                     settlement=settle, lot_sizes={CODE: lot}).results[0]
        assert not x.rejected, x.rejected


def test_p1_11_human_plans_share_one_budget_truncated_in_a_fixed_order(tmp_path):
    """审核 P1-11：两条买入计划各自都在预算内、合计超预算；协议选「截断」时按固定顺序（买单按代码）截断第二条，不留给撮合整单拒绝。"""
    from datetime import timedelta

    from mystock2.coach.intents import record_intent
    from mystock2.core import calendars as cal
    from mystock2.core import db as dbmod
    from mystock2.instruments.security_rule import SecurityRule, parse_bands
    from mystock2.scoreboard.providers import HumanPlanProvider

    fees = [FeeRule("syn", "US", "ANY", "order", "USD", pct_fee=Decimal("0.001"), min_fee=Decimal("1"))]
    tgt, p = DAYS[0], tmp_path / "c.db"
    dl = cal.project_deadline("US", tgt)
    dbmod.migrate(p)
    w = dbmod.connect_writer(p, "coach")
    st = LineState("USD", Decimal(10000))
    for code in (CODE, CODE2):
        record_intent(w, batch_id="B", line_id="B:human_plan", market="US", code=code, target_session=tgt, action="BUY", limit_price="10", qty=900,
                      state=st, state_hash=st.hash(), now=dl - timedelta(hours=2), deadline_at=dl, constraint_handling="truncate",
                      rule=SecurityRule(code, "2026-01-01", None, 1, parse_bands('[{"tick":"0.01"}]'), "合成", True), fee_rules=fees)
    md = FakeMD()
    md.flat(CODE, DAYS[:1], 10)
    md.flat(CODE2, DAYS[:1], 10)
    for handling, second in (("truncate", True), ("reject", False)):
        prov = HumanPlanProvider(w, batch_id="B", line_id="B:human_plan", market="US", codes=[CODE, CODE2], fee_rules=fees,
                                 lot_sizes={CODE: 1, CODE2: 1}, constraint_handling=handling)
        res = run_line(md, market="US", currency="USD", initial=st, sessions=[tgt], provider=prov, protocol=PROTO, fee_rules=fees,
                       settlement=SettlementRule("US", 1), lot_sizes={CODE: 1, CODE2: 1}).results[0]
        assert not res.rejected
        got = {f.code: f.qty for f in res.fills}
        first, other = sorted((CODE, CODE2))
        assert got[first] == 900 and (0 < got.get(other, 0) < 900) == second
        assert prov.flags[(tgt, other)] == ["group_budget_truncated" if second else "group_budget_exceeded"]


def test_p1_12_human_actual_books_cash_of_non_session_events_on_the_next_session(tmp_path):
    """审核 P1-12：美东周日 20:30 夜盘买入 100@10：现金必须在下一个交易日扣减（与持仓同一切分），权益不得虚增 1000。"""
    from tests.unit.ledger_helpers import make_db

    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, "2026-02-27T00:00:00.000000Z", {}, {"USD": "100000"})
    md = FakeMD()
    md.flat(CODE, DAYS, 10)
    post_event(conn, EventDraft(fill_key(ACCT, "N1"), ACCT, "FILL", "2026-03-09T00:30:00.000000Z", "USD", code=CODE, price="10", qty_delta="100",
                                cash_delta="-1000", ref_deal_id="N1"))
    res = human_actual_series(conn, md, account_id=ACCT, market="US", currency="USD", codes={CODE}, budget=D(1000), d0=DAYS[0], sessions=DAYS[1:7])
    assert all(r.equity == D(1000) for r in res), [(r.date, r.cash, r.positions, r.equity) for r in res]
    held = [r for r in res if r.positions]
    assert held and held[0].cash == D(0) and held[0].fills
