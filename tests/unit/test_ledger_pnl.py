"""已实现盈亏（移动平均成本法）：合成数据，手算期望值（合成测试值）。"""
from decimal import Decimal as D

import pytest

from mystock2.ledger import pnl
from mystock2.ledger.pnl import BUY, OPENING, SELL, SPLIT, TradeEvent, compute_realized_pnl, trade_net_cashflow

T0 = "2026-03-02T00:00:00.000000Z"


def ev(kind, at, qty="0", price=None, fee="0", ref="", code="US.NVDA", ccy="USD", **kw):
    return TradeEvent(kind, code, ccy, at, D(qty), D(price) if price is not None else None, D(fee), ref, **kw)


def test_moving_average_exact_with_fees():
    r = compute_realized_pnl([
        ev(BUY, "2026-03-03T15:00:00.000000Z", "100", "10", "1", "b1"),
        ev(BUY, "2026-03-04T15:00:00.000000Z", "100", "12", "0", "b2"),
        ev(SELL, "2026-03-05T15:00:00.000000Z", "50", "15", "1", "s1"),
    ], T0)
    # 平均成本 = (1000+1+1200)/200 = 11.005；已实现 = 750 − 550.25 − 1 = 198.75
    s = r.sells[0]
    assert s.quality == pnl.EXACT and s.avg_cost == D("11.005") and s.realized == D("198.75")
    c = r.by_code["US.NVDA"]
    assert c.realized_exact == D("198.75") and c.realized_estimated == 0 and c.unavailable_qty == 0
    assert c.qty == 150 and c.avg_cost == D("11.005") and not c.cost_estimated
    assert c.fees_total == D("2")


def test_opening_lot_with_snapshot_cost_is_estimated():
    r = compute_realized_pnl([ev(OPENING, T0, "100", "8"), ev(SELL, "2026-03-03T15:00:00.000000Z", "40", "10", "0", "s1")], T0)
    s = r.sells[0]
    assert s.quality == pnl.ESTIMATED and s.realized == D("80")
    c = r.by_code["US.NVDA"]
    assert c.realized_estimated == D("80") and c.realized_exact == 0 and c.cost_estimated
    assert c.qty == 60 and c.avg_cost == 8


def test_opening_lot_without_cost_is_unavailable_not_zero():
    """T-01：开账持仓没有成本证据，卖出盈亏不可用（None），不是 0。"""
    r = compute_realized_pnl([ev(OPENING, T0, "100"), ev(SELL, "2026-03-03T15:00:00.000000Z", "40", "10", "1", "s1")], T0)
    s = r.sells[0]
    assert s.quality == pnl.UNAVAILABLE and s.realized is None and s.avg_cost is None
    assert s.unavailable_qty == 40 and s.net_proceeds == D("399")
    c = r.by_code["US.NVDA"]
    assert c.realized_known == 0 and c.unavailable_qty == 40 and c.unavailable_net_proceeds == D("399")
    assert c.avg_cost is None and c.unknown_qty == 60


def test_mixed_unknown_and_exact_pools_share_proportionally():
    r = compute_realized_pnl([
        ev(OPENING, T0, "100"),                               # 无成本证据
        ev(BUY, "2026-03-03T15:00:00.000000Z", "100", "10", "0", "b1"),
        ev(SELL, "2026-03-04T15:00:00.000000Z", "100", "12", "0", "s1"),   # 一半来自无成本证据
    ], T0)
    s = r.sells[0]
    assert s.quality == pnl.PARTIAL and s.known_qty == 50 and s.unavailable_qty == 50
    assert s.realized == D("100")                             # (12−10)×50
    c = r.by_code["US.NVDA"]
    assert c.qty == 100 and c.known_qty == 50 and c.unknown_qty == 50 and c.avg_cost == 10


def test_pre_opening_trades_are_descriptive_and_never_pnl():
    r = compute_realized_pnl([
        ev(BUY, "2026-02-01T15:00:00.000000Z", "100", "5", "0", "old-b"),
        ev(SELL, "2026-02-20T15:00:00.000000Z", "100", "9", "0", "old-s"),
        ev(SELL, T0, "1", "9", "0", "at-t0"),                  # 恰在开账时点：也是 pre_opening
    ], T0)
    assert [p.ref for p in r.pre_opening] == ["old-b", "old-s", "at-t0"]
    assert r.sells == [] and r.by_code == {}


def test_oversell_is_flagged_not_invented():
    r = compute_realized_pnl([
        ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "10", "0", "b"),
        ev(SELL, "2026-03-04T15:00:00.000000Z", "30", "11", "0", "s"),
    ], T0)
    s = r.sells[0]
    assert s.quality == pnl.PARTIAL and s.known_qty == 10 and s.unavailable_qty == 20 and s.realized == D("10")
    assert "oversold:US.NVDA" in r.warnings and "超出可追溯库存" in s.note


def test_split_scales_quantity_and_keeps_total_cost():
    r = compute_realized_pnl([
        ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "100", "0", "b"),
        TradeEvent(SPLIT, "US.NVDA", "USD", "2026-03-04T15:00:00.000000Z", ratio_num=2, ratio_den=1),
        ev(SELL, "2026-03-05T15:00:00.000000Z", "10", "60", "0", "s"),    # 拆股后 20 股、平均成本 50
    ], T0)
    s = r.sells[0]
    assert s.avg_cost == 50 and s.realized == D("100")
    assert r.by_code["US.NVDA"].qty == 10


def test_split_at_same_instant_applies_before_fill():
    """同一时刻先应用拆股、再处理成交：该时刻的成交已是拆股后的数量，不被缩放。"""
    r = compute_realized_pnl([
        ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "100", "0", "b"),
        ev(BUY, "2026-03-04T15:00:00.000000Z", "20", "50", "0", "b2"),
        TradeEvent(SPLIT, "US.NVDA", "USD", "2026-03-04T15:00:00.000000Z", ratio_num=2, ratio_den=1),
    ], T0)
    c = r.by_code["US.NVDA"]
    assert c.qty == 40 and c.avg_cost == 50          # 10→20 股（成本 1000）＋20 股（成本 1000）


def test_flat_position_resets_estimate_flag():
    r = compute_realized_pnl([
        ev(OPENING, T0, "10", "8"),
        ev(SELL, "2026-03-03T15:00:00.000000Z", "10", "9", "0", "s1"),     # 清仓：估算
        ev(BUY, "2026-03-04T15:00:00.000000Z", "10", "10", "0", "b"),
        ev(SELL, "2026-03-05T15:00:00.000000Z", "10", "12", "0", "s2"),    # 新一轮：精确
    ], T0)
    assert [s.quality for s in r.sells] == [pnl.ESTIMATED, pnl.EXACT]
    c = r.by_code["US.NVDA"]
    assert c.realized_estimated == 10 and c.realized_exact == 20 and not c.cost_estimated


def test_estimate_flag_persists_while_opening_shares_remain():
    r = compute_realized_pnl([
        ev(OPENING, T0, "100", "8"),
        ev(BUY, "2026-03-03T15:00:00.000000Z", "100", "10", "0", "b"),
        ev(SELL, "2026-03-04T15:00:00.000000Z", "100", "12", "0", "s"),    # 平均成本 9，仍混有估算成本
    ], T0)
    assert r.sells[0].quality == pnl.ESTIMATED and r.sells[0].realized == D("300")
    assert r.by_code["US.NVDA"].cost_estimated


def test_currencies_never_added_in_totals():
    r = compute_realized_pnl([
        ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "10", "0", "b1"),
        ev(SELL, "2026-03-04T15:00:00.000000Z", "10", "12", "0", "s1"),
        ev(BUY, "2026-03-03T15:00:00.000000Z", "100", "300", "0", "b2", code="HK.00700", ccy="HKD"),
        ev(SELL, "2026-03-04T15:00:00.000000Z", "100", "310", "0", "s2", code="HK.00700", ccy="HKD"),
    ], T0)
    t = r.totals_by_currency()
    assert t["USD"]["realized_exact"] == 20 and t["HKD"]["realized_exact"] == 1000


def test_net_cashflow_is_not_pnl():
    """成交净现金流只是现金流水：买入未卖出时为负，但没有亏损。"""
    evs = [ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "10", "1", "b")]
    assert trade_net_cashflow(evs) == {"USD": D("-101")}
    r = compute_realized_pnl(evs, T0)
    assert r.totals_by_currency()["USD"]["realized_exact"] == 0
    assert "pnl" not in trade_net_cashflow.__name__ and "profit" not in trade_net_cashflow.__name__
    evs.append(ev(SELL, "2026-03-04T15:00:00.000000Z", "10", "12", "1", "s"))
    assert trade_net_cashflow(evs) == {"USD": D("18")}      # −101 + 119


def test_no_opening_warns_and_treats_everything_as_forward():
    r = compute_realized_pnl([ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "10", "0", "b"), ev(SELL, "2026-03-04T15:00:00.000000Z", "5", "11", "0", "s")])
    assert "no_opening" in r.warnings and r.sells[0].realized == 5


def test_input_order_does_not_matter():
    a = [ev(BUY, "2026-03-03T15:00:00.000000Z", "10", "10", "0", "b"), ev(SELL, "2026-03-04T15:00:00.000000Z", "5", "11", "0", "s")]
    assert compute_realized_pnl(a, T0).sells[0].realized == compute_realized_pnl(list(reversed(a)), T0).sells[0].realized == 5


def test_validation_rejects_bad_inputs():
    with pytest.raises(pnl.PnlError):
        compute_realized_pnl([ev(BUY, "2026-03-03T15:00:00.000000Z", "0", "10")], T0)
    with pytest.raises(pnl.PnlError):
        compute_realized_pnl([ev(SELL, "2026-03-03T15:00:00.000000Z", "1", None)], T0)
    with pytest.raises(pnl.PnlError):
        compute_realized_pnl([ev(BUY, "2026-03-03T15:00:00.000000Z", "1", "10", "-1")], T0)
    with pytest.raises(pnl.PnlError):
        compute_realized_pnl([TradeEvent("HOLD", "US.NVDA", "USD", T0)], T0)


def test_l05_proportional_split_leaves_no_residual_shares_or_false_oversell():
    """审核 L-05：开账 2 股无成本＋买 1 股，三次各卖 1 股清仓：不得误报超卖、不得残留 1e-39 级股数；之后再买卖口径为「精确」。"""
    t = lambda h: f"2026-03-03T{h:02d}:00:00Z"  # noqa: E731
    evs = [TradeEvent(OPENING, "US.X", "USD", "2026-03-02T00:00:00Z", D(2), None), TradeEvent(BUY, "US.X", "USD", t(14), D(1), D(10))]
    evs += [TradeEvent(SELL, "US.X", "USD", t(15 + i), D(1), D(11)) for i in range(3)]
    evs += [TradeEvent(BUY, "US.X", "USD", t(19), D(5), D(10)), TradeEvent(SELL, "US.X", "USD", t(20), D(5), D(12))]
    r = compute_realized_pnl(evs, "2026-03-02T00:00:00Z")
    c = r.by_code["US.X"]
    assert not r.warnings and c.qty == 0 and c.known_qty == 0 and c.unknown_qty == 0
    assert sum(s.unavailable_qty for s in r.sells[:3]) == D(2) and r.sells[-1].quality == "exact" and r.sells[-1].realized == D(10)
