"""代码评审（gpt-6.1，2026-10-05）发现问题的回归测试。"""
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mystock2.core import db as dbmod
from mystock2.core.timeutil import iso_utc
from mystock2.ledger import opening
from mystock2.ledger.events import (
    EventDraft,
    LedgerError,
    cancel_fx,
    correct_event,
    fill_key,
    post_dividend,
    post_event,
    post_fx,
)
from mystock2.ledger.projection import project
from mystock2.ledger.settlement import SettlementRule, unsettled_sell_proceeds
from mystock2.market.bars import DailyBar, HourlyBar, put_daily, put_hourly
from mystock2.market.fx import put_rate
from tests.unit.ledger_helpers import ACCT, D1, D2, T0, buy, make_db, sell

D = Decimal
UTC = timezone.utc


def test_iso_utc_is_fixed_precision_and_lexicographic_order_equals_time_order():
    a, b = datetime(2026, 3, 5, 14, 0, 0, tzinfo=UTC), datetime(2026, 3, 5, 14, 0, 0, 100000, tzinfo=UTC)
    assert iso_utc(a) == "2026-03-05T14:00:00.000000Z" and iso_utc(b) == "2026-03-05T14:00:00.100000Z"
    assert iso_utc(a) < iso_utc(b)                                              # 同一秒内字典序＝时间序
    assert iso_utc("2026-03-05T14:00:00Z") == iso_utc(a) and iso_utc("2026-03-05T22:00:00+08:00") == iso_utc(a)


# ---------------------------------------------------------------- F05 开账更正与整包冻结
def test_f05_opening_correction_nets_out_and_package_is_frozen(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10"}, {"USD": "1000"})
    key = f"opening:{ACCT}:cash:USD"
    correct_event(conn, key, EventDraft(key, ACCT, "OPENING_CASH", T0, "USD", cash_delta="900"), "fix-1")
    p = project(conn, ACCT)
    assert p.cash == {"USD": D(900)}                                           # 不是 1,900：冲销按「被冲销事件类型」参与开账边界
    assert opening.record_opening  # noqa: B018
    with pytest.raises(LedgerError, match="开账包已冻结"):
        opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10", "US.TSLA": "5"}, {"USD": "1000"})        # 同 t0 追加新项目
    with pytest.raises(LedgerError, match="开账包已冻结"):
        opening.record_opening(conn, ACCT, T0, {"US.NVDA": "10"}, {"USD": "1000", "HKD": "1"})


# ---------------------------------------------------------------- F06 FX 成组
def test_f06_fx_group_cannot_be_half_corrected_and_cancel_reverses_both_legs(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {}, {"USD": "1000", "HKD": "0"})
    post_fx(conn, ACCT, "G1", D1, "USD", "100", "HKD", "780")
    with pytest.raises(LedgerError, match="成组"):
        correct_event(conn, f"fx:{ACCT}:G1:in", None, "r1")                     # 不能只冲销一腿
    assert project(conn, ACCT).cash == {"USD": D(900), "HKD": D(780)}
    cancel_fx(conn, ACCT, "G1", "c1")
    assert project(conn, ACCT).cash == {"USD": D(1000)}                          # 两腿一起冲销
    assert cancel_fx(conn, ACCT, "G1", "c1-again") == []                         # 已冲销：不再重复冲销（幂等空操作）
    assert project(conn, ACCT).cash == {"USD": D(1000)}


# ---------------------------------------------------------------- F07 股息支付必须结清
def test_f07_direct_dividend_payment_must_clear_the_whole_accrual(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, T0, {"US.NVDA": "100"}, {"USD": "0"})
    post_dividend(conn, ACCT, "DV1", "US.NVDA", "USD", accrual_at=D1, gross="100")
    base = f"div:{ACCT}:DV1"
    with pytest.raises(LedgerError, match="结清"):
        post_event(conn, EventDraft(f"{base}:payment", ACCT, "DIVIDEND_PAYMENT", D2, "USD", code="US.NVDA", cash_delta="90", recv_delta="-90", group_id="DV1"))
    with pytest.raises(LedgerError, match="找不到同组的计提"):
        post_event(conn, EventDraft("div:x:payment", ACCT, "DIVIDEND_PAYMENT", D2, "USD", code="US.NVDA", cash_delta="10", recv_delta="-10", group_id="NOPE"))
    with pytest.raises(LedgerError, match="不一致"):
        post_event(conn, EventDraft(f"{base}:payment", ACCT, "DIVIDEND_PAYMENT", D2, "USD", code="US.TSLA", cash_delta="100", recv_delta="-100", group_id="DV1"))
    post_event(conn, EventDraft(f"{base}:payment", ACCT, "DIVIDEND_PAYMENT", D2, "USD", code="US.NVDA", cash_delta="100", recv_delta="-100", group_id="DV1"))
    assert "USD" not in project(conn, ACCT).receivable


# ---------------------------------------------------------------- F08 结算与更正/开账边界一致
def test_f08_unsettled_proceeds_follow_corrections_cancellations_and_opening_boundary(tmp_path):
    conn = make_db(tmp_path)
    opening.record_opening(conn, ACCT, "2026-03-01T00:00:00.000000Z", {"US.NVDA": "20"}, {"USD": "0"})
    sell(conn, "OLD", "US.NVDA", 5, "10", "2026-02-20T15:00:00.000000Z")                    # 开账前历史：不计
    sell(conn, "S1", "US.NVDA", 10, "50", "2026-03-04T15:00:00.000000Z")
    key = fill_key(ACCT, "S1")
    rules = {"US": SettlementRule("US", 1)}
    asof = datetime(2026, 3, 4, 20, 0, tzinfo=UTC)
    assert unsettled_sell_proceeds(conn, ACCT, rules, asof) == {"USD": D(500)}
    correct_event(conn, key, EventDraft(key, ACCT, "FILL", "2026-03-04T15:00:00.000000Z", "USD", code="US.NVDA", price="40", qty_delta="-10", cash_delta="400",
                                        ref_deal_id="S1"), "c1")
    assert unsettled_sell_proceeds(conn, ACCT, rules, asof) == {"USD": D(400)}               # 不是 900
    correct_event(conn, key, None, "c2")
    assert unsettled_sell_proceeds(conn, ACCT, rules, asof) == {}                            # 取消后不再冻结
    _ = (buy, D2)


# ---------------------------------------------------------------- F20/F21 行情版本
def test_f20_quality_upgrade_creates_new_version_and_f21_versions_are_global_across_sources(tmp_path):
    p = tmp_path / "m.db"
    dbmod.migrate(p)
    m = dbmod.connect_writer(p, "market")
    bar = DailyBar("US.NVDA", date(2026, 3, 4), "10", "11", "9", "10.5", "10.5", "1")
    assert put_daily(m, [bar], source="a", quality="partial")["inserted"] == 1
    assert put_daily(m, [bar], source="a", quality="ok")["new_version"] == 1                  # partial→ok：相同 OHLCV 也追加终值版本
    assert put_daily(m, [bar], source="a", quality="ok")["duplicate"] == 1
    assert put_daily(m, [bar], source="b", quality="ok")["new_version"] == 1                  # 换来源：版本号全局递增，不触发主键冲突
    assert [r["version"] for r in m.execute("SELECT version FROM quote_daily ORDER BY version")] == [1, 2, 3]
    hb = HourlyBar("US.NVDA", datetime(2026, 3, 4, 14, 30, tzinfo=UTC), datetime(2026, 3, 4, 15, 30, tzinfo=UTC), "10", "11", "9", "10", "5", True)
    put_hourly(m, [hb], source="a")
    assert put_hourly(m, [hb], source="b")["new_version"] == 1
    put_rate(m, "USDHKD", date(2026, 3, 4), "7.8", source="a", event_at=datetime(2026, 3, 4, tzinfo=UTC))
    assert put_rate(m, "USDHKD", date(2026, 3, 4), "7.8", source="b", event_at=datetime(2026, 3, 4, tzinfo=UTC)) == "new_version"


# ---------------------------------------------------------------- F26 eval_run 冻结
def test_f26_eval_run_is_frozen_after_metrics_written(tmp_path):
    p = tmp_path / "s.db"
    dbmod.migrate(p)
    w = dbmod.connect_writer(p, "scoreboard")
    w.execute("INSERT INTO comparison_batch(batch_id, protocol_version, start_date, currency, e0, initial_state_json, state_hash, lines_json, created_at) "
              "VALUES ('B','p','2026-03-02','USD','1','{}','h','[]','t')")
    w.execute("INSERT INTO eval_run(run_id, batch_id, protocol_version, protocol_json, evidence_json, created_at) VALUES ('r','B','p','{}','[]','t')")
    w.execute("UPDATE eval_run SET metrics_json='{}' WHERE run_id='r'")                     # 首次写入指标：允许
    for sql in ("UPDATE eval_run SET metrics_json='{\"x\":1}'", "UPDATE eval_run SET protocol_json='{\"x\":1}'"):
        with pytest.raises(sqlite3.DatabaseError, match="冻结后只读"):
            w.execute(sql)


# ---------------------------------------------------------------- 复合写入者
def test_composite_veto_writer_can_write_tickets_and_llm_calls_but_not_ledger(tmp_path):
    p = tmp_path / "v.db"
    dbmod.migrate(p)
    w = dbmod.connect_writer(p, "veto")
    with pytest.raises(sqlite3.DatabaseError):
        w.execute("INSERT INTO ledger_event(event_id) VALUES ('x')")
    with pytest.raises(sqlite3.DatabaseError):
        w.execute("INSERT INTO sleeve_daily(run_id) VALUES ('x')")
    with pytest.raises(dbmod.DbError):
        dbmod.connect_writer(p, "nonsense")
    _ = timedelta


def test_f24_correction_request_id_cannot_be_reused_for_different_content(tmp_path):
    conn = make_db(tmp_path)
    buy(conn, "D-1", "US.NVDA", 10, "10", D1)
    key = fill_key(ACCT, "D-1")

    def draft(q):
        return EventDraft(key, ACCT, "FILL", D1, "USD", code="US.NVDA", price="10", qty_delta=str(q), cash_delta=str(-q * 10), ref_deal_id="D-1")
    ids = correct_event(conn, key, draft(12), "req-X")
    assert correct_event(conn, key, draft(12), "req-X") == ids                              # 同内容：幂等
    with pytest.raises(Exception, match="内容不同"):
        correct_event(conn, key, draft(8), "req-X")                                         # 同 id 不同内容：拒绝，不静默返回旧结果
    with pytest.raises(Exception, match="内容不同"):
        correct_event(conn, key, None, "req-X")
    assert project(conn, ACCT).positions["US.NVDA"] == 12
