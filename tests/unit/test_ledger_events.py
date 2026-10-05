import sqlite3
from decimal import Decimal

import pytest

from mystock2.core import db as dbmod
from mystock2.ledger import opening
from mystock2.ledger.events import (
    EventDraft,
    IdentityInsufficient,
    LedgerConflict,
    LedgerError,
    correct_event,
    fill_key,
    open_pending,
    post_dividend,
    post_event,
    post_fx,
    queue_pending,
    resolve_pending,
)
from mystock2.ledger.projection import effective_events, incomplete_fx_groups, project
from tests.unit.ledger_helpers import ACCT, D1, D2, T0, buy, fee, make_db, sell, src


@pytest.fixture()
def conn(tmp_path):
    return make_db(tmp_path)


# ---------------------------------------------------------------- 校验
def test_validation_rules(conn):
    def bad(**kw):
        base = dict(business_key="k", account_id=ACCT, event_type="FILL", event_at=D1, currency="USD", code="US.NVDA", price="10",
                    qty_delta="10", cash_delta="-100")
        base.update(kw)
        with pytest.raises(LedgerError):
            post_event(conn, EventDraft(**base))
    bad(price=None)                                  # 成交必须带价
    bad(cash_delta="-90")                            # 现金与 qty×price 不符
    bad(currency="HKD")                              # 币种与标的不一致
    bad(code="NVDA")                                 # 裸代码
    bad(event_at="2026-03-03T15:00:00")              # naive 时间
    bad(qty_delta="0")
    bad(event_type="REVERSAL")                       # 不允许直接写冲销
    bad(event_type="FEE", cash_delta="5", qty_delta="0")   # 费用不能是收入
    bad(event_type="FEE", cash_delta="-5", qty_delta="0")  # 费用必须归属
    bad(event_type="ADJUST", cash_delta="5", qty_delta="0", code=None, price=None)   # ADJUST 缺分类
    bad(event_type="DEPOSIT", cash_delta="-5", qty_delta="0", code=None, price=None)
    bad(account_id="NOPE")
    with pytest.raises(LedgerError):
        post_event(conn, EventDraft("k2", ACCT, "ADJUST", D1, "USD", cash_delta="5", adjust_class="EXTERNAL_FLOW", note=" "))


def test_float_amounts_cannot_enter(conn):
    with pytest.raises(LedgerError):
        post_event(conn, EventDraft("k", ACCT, "DEPOSIT", D1, "USD", cash_delta=0.1))   # type: ignore[arg-type]


# ---------------------------------------------------------------- T-03 / T-04
def test_t03_two_partial_fills_and_late_fee(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "10000"})
    buy(conn, "D-1", "US.NVDA", 60, "100", D1)
    buy(conn, "D-2", "US.NVDA", 40, "101", D1)
    fee(conn, "D-1", "1.00", D1)
    p = project(conn, ACCT)
    assert p.positions == {"US.NVDA": Decimal(100)}
    assert p.cash["USD"] == Decimal("10000") - 6000 - 4040 - 1          # 费用尚缺 D-2
    fee(conn, "D-2", "1.00", D2)                                         # 费用晚到并归属 D-2
    p = project(conn, ACCT)
    assert p.cash["USD"] == Decimal("10000") - 10040 - 2
    assert p.fees["USD"] == Decimal("-2")


def test_t04_cancelled_unfilled_orders_never_enter_and_correction_not_double_counted(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    buy(conn, "D-1", "US.NVDA", 5, "10", D1)
    # 撤单/失败/未成交：采集层不写事件 —— 账本里只有成交
    assert conn.execute("SELECT COUNT(*) c FROM ledger_event WHERE event_type='FILL'").fetchone()["c"] == 1
    # 手工更正（价格 10→11）：现金只体现一次差额
    correct_event(conn, fill_key(ACCT, "D-1"),
                  EventDraft(fill_key(ACCT, "D-1"), ACCT, "FILL", D1, "USD", code="US.NVDA", price="11", qty_delta="5", cash_delta="-55", ref_deal_id="D-1"),
                  "req-1")
    p = project(conn, ACCT)
    assert p.positions["US.NVDA"] == 5 and p.cash["USD"] == Decimal("1000") - 55


# ---------------------------------------------------------------- T-17 跨来源去重 / 待匹配
def test_t17_same_fill_from_three_channels_is_one_event_three_evidences(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    for source in ("v1", "futu", "csv"):
        r = buy(conn, "D-9", "US.NVDA", 5, "10", D1, source=src(source, "D-9", deal="D-9", src=source))
        assert r.status in ("inserted", "duplicate")
    assert conn.execute("SELECT COUNT(*) c FROM ledger_event WHERE business_key=?", (fill_key(ACCT, "D-9"),)).fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM source_link").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM source_record").fetchone()["c"] == 3
    assert project(conn, ACCT).positions == {"US.NVDA": Decimal(5)}


def test_t17_identity_insufficient_goes_pending_not_ledger(conn):
    with pytest.raises(IdentityInsufficient):
        fill_key(ACCT, None)
    pid = queue_pending(conn, src("csv", "row-17", price="10", qty="5"), "缺少 deal_id")
    assert len(open_pending(conn)) == 1
    assert conn.execute("SELECT COUNT(*) c FROM ledger_event").fetchone()["c"] == 0
    resolve_pending(conn, pid, "rejected", note="无法确认")
    assert open_pending(conn) == []
    with pytest.raises(sqlite3.IntegrityError):                       # 同一待匹配只能解决一次
        resolve_pending(conn, pid, "duplicate")


def test_differing_metadata_across_channels_is_not_a_conflict_but_differing_economics_is(conn):
    from dataclasses import replace
    d = EventDraft(fill_key(ACCT, "D-7"), ACCT, "FILL", D1, "USD", code="US.NVDA", price="10", qty_delta="5", cash_delta="-50", ref_deal_id="D-7", ref_order_id="O-1", note="futu")
    assert post_event(conn, d).status == "inserted"
    other_channel = replace(d, ref_order_id=None, note="csv")          # 辅助元数据不同（订单号、备注）
    assert post_event(conn, other_channel).status == "duplicate"
    assert conn.execute("SELECT note FROM ledger_event WHERE business_key=?", (fill_key(ACCT, "D-7"),)).fetchone()["note"] == "futu"   # 首次写入者的元数据保留
    with pytest.raises(LedgerConflict):
        post_event(conn, replace(d, price="11", cash_delta="-55"))        # 经济字段不同才是真冲突


def test_conflicting_duplicate_is_an_error_not_silently_merged(conn):
    buy(conn, "D-1", "US.NVDA", 5, "10", D1)
    with pytest.raises(LedgerConflict):
        buy(conn, "D-1", "US.NVDA", 5, "11", D1)


# ---------------------------------------------------------------- T-18 更正链
def test_t18_correction_chain_atomic_idempotent_no_double_reversal(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    key = fill_key(ACCT, "D-1")
    buy(conn, "D-1", "US.NVDA", 10, "10", D1)

    def draft(q, px):
        return EventDraft(key, ACCT, "FILL", D1, "USD", code="US.NVDA", price=str(px), qty_delta=str(q), cash_delta=str(-Decimal(q) * Decimal(px)), ref_deal_id="D-1")

    ids1 = correct_event(conn, key, draft(12, 10), "req-A")
    assert ids1 == [f"{key}#2", f"{key}#3"]
    assert correct_event(conn, key, draft(12, 10), "req-A") == ids1       # 幂等
    ids2 = correct_event(conn, key, draft(8, 10), "req-B")                # 第二次更正针对当前有效版本（#3）
    assert ids2 == [f"{key}#4", f"{key}#5"]
    p = project(conn, ACCT)
    assert p.positions["US.NVDA"] == 8 and p.cash["USD"] == Decimal("1000") - 80
    # 有效版本只有最后一个
    assert [r["event_id"] for r in effective_events(conn, ACCT) if r["business_key"] == key] == [f"{key}#5"]
    # 同一旧版本不得被冲销两次（库层唯一索引）
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ledger_event(event_id,business_key,event_version,account_id,event_type,event_at,received_at,currency,corrects_event_id,content_hash,created_at,qty_delta,cash_delta) "
                     "VALUES ('x#9','x',9,?,'REVERSAL',?,?,'USD',?, 'h','t','0','0')", (ACCT, D1, D1, f"{key}#3"))
    # 取消
    correct_event(conn, key, None, "req-C")
    assert project(conn, ACCT).positions == {} and project(conn, ACCT).cash["USD"] == Decimal(1000)
    with pytest.raises(LedgerError):
        correct_event(conn, key, None, "req-D")                           # 已取消
    # 同一 request id 不得用于别的业务事件
    with pytest.raises(LedgerConflict):
        buy(conn, "D-2", "US.NVDA", 1, "10", D1)
        correct_event(conn, fill_key(ACCT, "D-2"), None, "req-A")


def test_append_only_triggers(conn):
    buy(conn, "D-1", "US.NVDA", 1, "10", D1, source=src("futu", "D-1"))
    for sql in ("UPDATE ledger_event SET cash_delta='0'", "DELETE FROM ledger_event", "UPDATE source_record SET source='x'",
                "DELETE FROM source_link"):
        with pytest.raises(sqlite3.DatabaseError, match="只追加|不可变"):
            conn.execute(sql)


# ---------------------------------------------------------------- T-19 FX 两腿
def test_t19_fx_both_legs_atomic_and_same_group_not_duplicates(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000", "HKD": "0"})
    res = post_fx(conn, ACCT, "G1", D1, "USD", "100", "HKD", "780")
    assert [r.status for r in res] == ["inserted", "inserted"]
    assert [r.status for r in post_fx(conn, ACCT, "G1", D1, "USD", "100", "HKD", "780")] == ["duplicate", "duplicate"]
    p = project(conn, ACCT)
    assert p.cash == {"USD": Decimal(900), "HKD": Decimal(780)}
    assert incomplete_fx_groups(conn, ACCT) == []
    # 第二腿非法（金额为 0）→ 整组回滚
    with pytest.raises(LedgerError):
        post_fx(conn, ACCT, "G2", D1, "USD", "100", "HKD", "0")
    assert conn.execute("SELECT COUNT(*) c FROM ledger_event WHERE group_id='G2'").fetchone()["c"] == 0
    # 人为制造只有一腿：被检出
    post_event(conn, EventDraft(f"fx:{ACCT}:G3:out", ACCT, "FX", D1, "USD", cash_delta="-5", group_id="G3", leg_id="out"))
    assert incomplete_fx_groups(conn, ACCT) == ["G3"]


# ---------------------------------------------------------------- T-01 开账边界
def test_t01_events_before_opening_are_descriptive_only(conn):
    # 开账日之前的历史成交（含一笔卖出）：不进入前向和式
    buy(conn, "H-1", "US.NVDA", 100, "10", "2026-02-01T15:00:00Z")
    sell(conn, "H-2", "US.NVDA", 30, "12", "2026-02-10T15:00:00Z")
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "70"}, {"USD": "500"})
    p = project(conn, ACCT)
    assert p.positions == {"US.NVDA": Decimal(70)} and p.cash == {"USD": Decimal(500)}
    assert p.pre_opening_events == 2
    buy(conn, "N-1", "US.NVDA", 10, "10", D1)
    assert project(conn, ACCT).positions["US.NVDA"] == 80
    with pytest.raises(LedgerError):
        opening.record_opening(conn, ACCT, "2026-03-09T00:00:00Z", {}, {})   # 开账点不可改
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "70"}, {"USD": "500"})  # 相同内容幂等


def test_no_opening_warning(conn):
    buy(conn, "N-1", "US.NVDA", 10, "10", D1)
    assert "no_opening" in project(conn, ACCT).warnings


# ---------------------------------------------------------------- T-06 拆股、T-22 股息
def test_t06_split_factor_projection_and_late_correction(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "10000"})
    split_at = "2026-03-10T00:00:00Z"
    buy(conn, "D-1", "US.NVDA", 10, "100", D1)
    opening.add_split(conn, "US.NVDA", split_at, 2, 1)
    assert project(conn, ACCT, as_of="2026-03-09T00:00:00Z").positions["US.NVDA"] == 10      # 拆股前
    assert project(conn, ACCT).positions["US.NVDA"] == 20
    key = fill_key(ACCT, "D-1")
    correct_event(conn, key, EventDraft(key, ACCT, "FILL", D1, "USD", code="US.NVDA", price="100", qty_delta="12", cash_delta="-1200", ref_deal_id="D-1"), "r1")
    assert project(conn, ACCT).positions["US.NVDA"] == 24                                    # 10→12 股，1:2 拆股后 24（不是 22）
    assert opening.add_split(conn, "US.NVDA", split_at, 2, 1) == f"split:US.NVDA:{split_at.replace('+00:00', 'Z')}"   # 幂等
    with pytest.raises(LedgerError):
        opening.add_split(conn, "US.NVDA", split_at, 3, 1)


def test_t06_same_instant_split_does_not_scale_the_fill(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "10000"})
    at = "2026-03-10T00:00:00Z"
    opening.add_split(conn, "US.NVDA", at, 2, 1)
    buy(conn, "D-1", "US.NVDA", 10, "50", at)         # 与拆股同刻：先应用公司行动，再处理成交 → 成交按拆股后单位
    assert project(conn, ACCT).positions["US.NVDA"] == 10


def test_reverse_split_fractional_flagged(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000"})
    buy(conn, "D-1", "US.NVDA", 10, "10", D1)
    opening.add_split(conn, "US.NVDA", D2, 1, 3)       # 1:3 合股 → 3.333…
    assert any(w.startswith("fractional_position") for w in project(conn, ACCT).warnings)


@pytest.mark.parametrize("scenario", ["gross_and_tax", "net_and_tax", "net_only"])
def test_t22_dividend_three_data_cases_same_economics(conn, scenario):
    """总额 100、预扣税 10：三种数据情形期末应收均为 0、现金净增 90、支付日权益变化 −10。"""
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "100"}, {"USD": "0"})
    ex, pay = "2026-03-10T00:00:00Z", "2026-03-20T00:00:00Z"
    if scenario == "gross_and_tax":
        post_dividend(conn, ACCT, "DV1", "US.NVDA", "USD", accrual_at=ex, gross="100", payment_at=pay, withholding_tax="10")
    elif scenario == "net_and_tax":
        net, tax = Decimal("90"), Decimal("10")
        post_dividend(conn, ACCT, "DV1", "US.NVDA", "USD", accrual_at=ex, gross=str(net + tax), payment_at=pay, withholding_tax=str(tax))
    else:
        post_dividend(conn, ACCT, "DV1", "US.NVDA", "USD", accrual_at=ex, gross="100", payment_at=pay, cash_received="90")
    before = project(conn, ACCT, as_of="2026-03-15T00:00:00Z")          # 除息后、支付前
    assert before.cash.get("USD", Decimal(0)) == 0 and before.receivable["USD"] == 100
    after = project(conn, ACCT)
    assert after.cash["USD"] == 90                                      # 不得为 80
    assert "USD" not in after.receivable                                # 期末应收 0
    def equity(p):   # 证券市值在两个时点相同，不影响差
        return p.cash.get("USD", Decimal(0)) + p.receivable.get("USD", Decimal(0))

    assert equity(after) - equity(before) == -10                        # 支付日权益变化 −10（税款/差额确认）
    if scenario == "net_only":
        assert after.attributed_shortfall["USD"] == 10 and after.taxes == {}
    elif scenario != "net_only":
        assert after.taxes["USD"] == -10


def test_dividend_validation(conn):
    with pytest.raises(LedgerError):
        post_dividend(conn, ACCT, "DV2", "US.NVDA", "USD", accrual_at=D1, gross="100", payment_at=D2, cash_received="90", withholding_tax="10")
    with pytest.raises(LedgerError):
        post_dividend(conn, ACCT, "DV3", "US.NVDA", "USD", accrual_at=D1, gross="100", payment_at=D2, cash_received="120")


# ---------------------------------------------------------------- 资金流
def test_t07_deposit_is_external_flow_not_return_and_adjust_needs_class(conn):
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "0"})
    post_event(conn, EventDraft("dep:1", ACCT, "DEPOSIT", D1, "USD", cash_delta="5000"))
    post_event(conn, EventDraft("adj:1", ACCT, "ADJUST", D1, "USD", cash_delta="100", adjust_class="EXTERNAL_FLOW", note="手工补录入金，凭证 X"))
    post_event(conn, EventDraft("adj:2", ACCT, "ADJUST", D1, "USD", cash_delta="7", adjust_class="INVESTMENT", note="利息补记"))
    p = project(conn, ACCT)
    assert p.cash["USD"] == 5107
    assert p.external_flow["USD"] == 5100                                # ADJUST/INVESTMENT 不计外部流
    post_event(conn, EventDraft("wd:1", ACCT, "WITHDRAW", D2, "USD", cash_delta="-1000"))
    assert project(conn, ACCT).external_flow["USD"] == 4100


# ---------------------------------------------------------------- 权限
def test_other_writers_cannot_touch_ledger(tmp_path):
    conn = make_db(tmp_path)
    buy(conn, "D-1", "US.NVDA", 1, "10", D1)
    path = tmp_path / "t.db"
    inst = dbmod.connect_writer(path, "instruments")
    with pytest.raises(sqlite3.DatabaseError):
        inst.execute("INSERT INTO ledger_event(event_id) VALUES ('z')")
    ro = dbmod.connect_ro(path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM ledger_event")
