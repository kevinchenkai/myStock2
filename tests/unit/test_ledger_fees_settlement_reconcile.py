from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mystock2.ledger import opening
from mystock2.ledger.events import EventDraft, post_event, queue_pending
from mystock2.ledger.fees import FeeRule, estimate, load_fee_rules, select_rule
from mystock2.ledger.projection import project
from mystock2.ledger.reconcile import reconcile
from mystock2.ledger.settlement import (
    SettlementRule,
    SettlementUnknown,
    settle_date,
    tradable_cash,
    unsettled_sell_proceeds,
)
from tests.unit.ledger_helpers import ACCT, D1, D2, D3, T0, buy, fee, make_db, sell, src

D = Decimal


# ---------------------------------------------------------------- 费用（T-21，合成费率）
def rule(**kw):
    base = dict(profile_id="synthetic", market="US", side="ANY", basis="order", currency="USD",
                pct_fee=D("0.001"), min_fee=D("1"), flat_fee=D("0"))
    base.update(kw)
    return FeeRule(**base)


def test_t21_min_fee_charged_once_per_order_vs_per_fill():
    fills = [(D(60), D("5")), (D(40), D("5"))]                     # 两次部分成交：300 + 200
    assert estimate(rule(basis="order"), fills).fee == D("1.00")   # 合并 500×0.001=0.5 → 最低费 1 只收一次
    assert estimate(rule(basis="fill"), fills).fee == D("2.00")    # 每笔各收最低费
    big = [(D(1000), D("50"))]
    assert estimate(rule(), big).fee == D("50.00")                  # 50000×0.001


def test_cap_tax_rounding_and_flat():
    r = rule(pct_fee=D("0.003"), min_fee=D("0"), cap_fee=D("10"), tax_pct=D("0.001"), flat_fee=D("0.5"), round_step=D("0.01"))
    e = estimate(r, [(D(1000), D("100"))])                          # 100000：fee=300.5→封顶 10；税=100
    assert (e.fee, e.tax, e.total) == (D("10.00"), D("100.00"), D("110.00"))
    assert estimate(rule(pct_fee=D("0.00025"), min_fee=D("0")), [(D(3), D("10.1"))]).fee == D("0.01")   # 0.007575 → 0.01


def test_rule_selection_by_side_and_validity_and_no_silent_zero():
    rules = [rule(side="ANY", profile_id="any"), rule(side="SELL", profile_id="sell", tax_pct=D("0.001")),
             rule(side="BUY", profile_id="old", valid_from="2020-01-01", valid_to="2026-01-01"),
             rule(side="BUY", profile_id="new", valid_from="2026-01-01")]
    assert select_rule(rules, "US", "SELL", "2026-03-01").profile_id == "sell"      # 精确方向优先于 ANY
    assert select_rule(rules, "US", "BUY", "2025-06-01").profile_id == "old"
    assert select_rule(rules, "US", "BUY", "2026-03-01").profile_id == "new"
    with pytest.raises(LookupError):
        select_rule(rules, "HK", "BUY", "2026-03-01")                              # 无适用档案：报错而非 0


def test_load_fee_rules_from_yaml(tmp_path):
    p = tmp_path / "fees.yaml"
    p.write_text("rules:\n  - {profile_id: syn, market: HK, side: BUY, basis: fill, currency: HKD, pct_fee: '0.0003', min_fee: '3', tax_pct: '0.001'}\n", encoding="utf-8")
    (r,) = load_fee_rules(p)
    assert r.market == "HK" and r.min_fee == D("3") and r.basis == "fill"


# ---------------------------------------------------------------- 结算（T-23）
def test_settle_date_requires_configured_rule_and_skips_non_sessions():
    with pytest.raises(SettlementUnknown):
        settle_date(SettlementRule("US", None), datetime(2025, 7, 3).date())
    # 合成规则 lag=1：2025-07-03（周四）→ 7/4 休市 → 下一个交易日 7/7
    assert settle_date(SettlementRule("US", 1), datetime(2025, 7, 3).date()).isoformat() == "2025-07-07"
    assert settle_date(SettlementRule("US", 2), datetime(2025, 7, 3).date()).isoformat() == "2025-07-08"
    assert settle_date(SettlementRule("US", 0), datetime(2025, 7, 3).date()).isoformat() == "2025-07-03"


def test_t23_sale_proceeds_not_tradable_until_settled(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10"}, {"USD": "100"})
    sell(conn, "S-1", "US.NVDA", 10, "50", "2026-03-04T15:00:00.000000Z")           # 周三
    econ = project(conn, ACCT).cash
    assert econ["USD"] == 600
    rules = {"US": SettlementRule("US", 1)}
    unsettled_wed = unsettled_sell_proceeds(conn, ACCT, rules, datetime(2026, 3, 4, 20, 0, tzinfo=timezone.utc))
    assert unsettled_wed == {"USD": D(500)}
    assert tradable_cash(econ, unsettled_wed)["USD"] == 100                     # 回款当日不可立即再用
    unsettled_thu = unsettled_sell_proceeds(conn, ACCT, rules, datetime(2026, 3, 5, 20, 0, tzinfo=timezone.utc))
    assert unsettled_thu == {}                                                  # 结算日当天视为已结算
    assert tradable_cash(econ, unsettled_thu, {"USD": D(50)})["USD"] == 550     # 再减去订单预留
    with pytest.raises(SettlementUnknown):                                      # 未配置结算规则：失败关闭
        unsettled_sell_proceeds(conn, ACCT, {}, datetime(2026, 3, 4, 20, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------- 对账（LG-07）
def snap(conn, at, positions, cash):
    return opening.create_snapshot(conn, ACCT, at, "futu",
                                   {c: {"qty": q} for c, q in positions.items()}, {k: {"cash": v} for k, v in cash.items()})


def test_reconcile_exact_match_and_diffs(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10"}, {"USD": "1000"})
    buy(conn, "D-1", "US.NVDA", 5, "20", D1)
    fee(conn, "D-1", "1.00", D1)
    ok = snap(conn, D2, {"US.NVDA": "15"}, {"USD": "899"})
    rep = reconcile(conn, ACCT, ok)
    assert rep.ok and not rep.position_diffs and not rep.cash_diffs
    assert snap(conn, D2, {"US.NVDA": "15"}, {"USD": "899"}) == ok            # 快照幂等
    bad = snap(conn, D3, {"US.NVDA": "14", "US.TSLA": "1"}, {"USD": "890", "HKD": "5"})
    rep = reconcile(conn, ACCT, bad)
    assert not rep.ok
    assert {d["code"] for d in rep.position_diffs} == {"US.NVDA", "US.TSLA"}     # 持仓数量必须逐标的一致
    assert {d["currency"] for d in rep.cash_diffs} == {"USD", "HKD"}


def test_reconcile_cash_within_tolerance_and_snapshot_time_cutoff(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    buy(conn, "D-1", "US.NVDA", 1, "10", D1)
    buy(conn, "D-2", "US.NVDA", 1, "10", D3)                                    # 快照之后的成交不应计入
    s = snap(conn, D2, {"US.NVDA": "1"}, {"USD": "990.005"})
    assert reconcile(conn, ACCT, s).ok                                          # 差 0.005 ≤ 默认容差 0.01
    assert not reconcile(conn, ACCT, s, cash_tolerance=D("0.001")).ok


def test_reconcile_known_cash_diff_baseline_is_listed_but_only_drift_fails(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    s = snap(conn, D2, {}, {"USD": "1013.84"})                                  # 账本 1000，券商 1013.84：差 −13.84
    assert not reconcile(conn, ACCT, s).ok                                      # 无基线：照常失败
    rep = reconcile(conn, ACCT, s, known_cash_diffs={"USD": D("-13.84")})
    assert rep.ok and not rep.cash_diffs                                        # 基线内：不算新差异
    assert rep.baseline_cash_diffs and rep.baseline_cash_diffs[0]["diff"] == "-13.84"   # 但仍列出，不隐藏
    drift = snap(conn, D3, {}, {"USD": "1013.90"})                              # 又多漂了 0.06
    rep = reconcile(conn, ACCT, drift, known_cash_diffs={"USD": D("-13.84")})
    assert not rep.ok and rep.cash_diffs[0]["baseline"] == "-13.84"
    other = snap(conn, D3, {}, {"USD": "1013.84", "HKD": "5"})                  # 基线只豁免所登记的币种
    assert {d["currency"] for d in reconcile(conn, ACCT, other, known_cash_diffs={"USD": D("-13.84")}).cash_diffs} == {"HKD"}


def test_reconcile_lists_pending_and_incomplete_fx_never_hides_them(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "100"})
    s = snap(conn, D2, {}, {"USD": "100"})
    assert reconcile(conn, ACCT, s).ok
    queue_pending(conn, src("csv", "r1"), "缺 deal_id")
    post_event(conn, EventDraft(f"fx:{ACCT}:G9:out", ACCT, "FX", D1, "USD", cash_delta="0.001", group_id="G9", leg_id="out"), _internal_fx=True)
    s2 = snap(conn, D3, {}, {"USD": "100.001"})
    rep = reconcile(conn, ACCT, s2)
    assert not rep.ok and rep.open_pending == 1 and rep.incomplete_fx_groups == ["G9"]


def test_reconcile_unknown_snapshot_raises(tmp_path):
    conn = make_db(tmp_path)
    with pytest.raises(ValueError):
        reconcile(conn, ACCT, "nope")


def test_date_only_v1_snapshot_cannot_be_reconciled(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10"}, {"USD": "1000"})
    sid = opening.create_snapshot(conn, ACCT, D2, "v1-date-only", {"US.NVDA": {"qty": "10"}}, {"USD": {"cash": "1000"}})
    with pytest.raises(ValueError, match="没有采集时刻"):
        reconcile(conn, ACCT, sid)                                                  # 当日 23:59:59Z 占位时刻会让盘中成交造成虚假差异


def test_derive_opening_from_a_later_snapshot_gives_exact_positions_and_residual_cash(tmp_path):
    from mystock2.ledger.opening import derive_opening

    conn = make_db(tmp_path)
    buy(conn, "D-1", "US.NVDA", 5, "20", D1)
    sell(conn, "D-2", "US.TSLA", 2, "100", D2)
    fee(conn, "D-1", "1.00", D1)
    snap_id = snap(conn, D3, {"US.NVDA": "15", "US.TSLA": "8"}, {"USD": "899"})
    pos, cash, warns = derive_opening(conn, ACCT, snap_id, T0)
    assert pos == {"US.NVDA": "10", "US.TSLA": "10"} and not warns            # 15−5、8+2
    assert cash == {"USD": "800"}                                              # 899 −(−100−1+200)＝800：倒推残差
    opening.record_opening(conn, ACCT, T0, pos, cash)
    assert reconcile(conn, ACCT, snap_id).ok                                   # 倒推开账后，到该快照时刻必然对得上（这是构造，不是独立证据）
    with pytest.raises(Exception, match="快照不存在"):
        derive_opening(conn, ACCT, "nope", T0)
    # 成交不完整（快照持仓小于成交累计）→ 期初为负，必须报警
    conn2 = make_db(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    buy(conn2, "D-9", "US.NVDA", 50, "20", D1)
    s2 = snap(conn2, D3, {"US.NVDA": "10"}, {"USD": "0"})
    _, _, w2 = derive_opening(conn2, ACCT, s2, T0)
    assert any("为负" in w for w in w2)


def test_derive_opening_ignores_broken_fx_group_like_projection(tmp_path):
    """缺腿的换汇组在 project 里整组不生效；倒推开账也不能把它当成真实现金变动（审核 P3）。"""
    from mystock2.ledger.opening import derive_opening

    conn = make_db(tmp_path)
    post_event(conn, EventDraft(f"fx:{ACCT}:g:in", ACCT, "FX", D1, "USD", cash_delta="100", group_id="g", leg_id="in"), _internal_fx=True)
    snap_id = snap(conn, D3, {}, {"USD": "500"})
    _, cash, _ = derive_opening(conn, ACCT, snap_id, T0)
    assert cash == {"USD": "500"}
