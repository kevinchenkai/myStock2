import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mystock2.coach.decide import BUY, HOLD, SKIP, TicketDraft
from mystock2.coach.intents import (
    IntentError,
    IntentRejected,
    first_reveal_at,
    plan_to_drafts,
    record_intent,
    reveal,
    select_human_plan,
)
from mystock2.coach.tickets import Cell, TicketError, coverage, freeze_tickets, select_ticket
from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.instruments.security_rule import SecurityRule, parse_bands
from mystock2.ledger.fees import FeeRule
from mystock2.market.bars import HourlyBar, put_hourly
from mystock2.scoreboard.engine import run_line
from mystock2.scoreboard.marketdata import DbMarketData
from mystock2.scoreboard.providers import HumanPlanProvider, TicketProvider
from mystock2.scoreboard.types import ExecProtocol, LineState
from tests.unit.coach_helpers import seed_prediction
from tests.unit.scoreboard_helpers import FakeMD, state

UTC = timezone.utc
D = Decimal
TARGET = date(2026, 3, 5)
DEADLINE = cal.project_deadline("US", TARGET)
RULE = SecurityRule("US.NVDA", "2026-01-01", None, 1, parse_bands('[{"tick":"0.01"}]'), "合成", True)
FEES = [FeeRule("syn", "US", "ANY", "order", "USD", pct_fee=D("0.001"), min_fee=D("1"))]
B, L = "B1", "B1:ai"


@pytest.fixture()
def conn(tmp_path):
    p = tmp_path / "c.db"
    dbmod.migrate(p)
    for code in ("US.NVDA", "US.TSLA", "US.AMD"):
        seed_prediction(p, f"p-{code}", code, TARGET.isoformat())
    return dbmod.connect_writer(p, "coach")


def draft(action=BUY, px="97", qty=30, code="US.NVDA", reasons=("edge_ok",)):
    return TicketDraft(code, action, D(px) if px else None, qty, 1, D("2913") if action == BUY else None, reasons, model_ref=f"p-{code}")


def freeze(conn, drafts, *, at, stage="close", state_ref="s0", line=L, kind="line_sim", unavailable=None, deadline=DEADLINE):
    return freeze_tickets(conn, batch_id=B, line_id=line, kind=kind, market="US", target_session=TARGET, stage=stage, drafts=drafts,
                          state_ref_type="line_state" if kind == "line_sim" else "account_snapshot", state_ref=state_ref, strategy_version="inv-policy-v1",
                          protocol_version="exec-v1", generated_at=at - timedelta(minutes=1), now=at, deadline_at=deadline, unavailable_reason=unavailable)


def cell(code="US.NVDA", line=L, kind="line_sim"):
    return Cell(B, line, kind, "US", TARGET.isoformat(), code)


BEFORE = DEADLINE - timedelta(hours=12)
BEFORE2 = DEADLINE - timedelta(minutes=20)
AFTER = DEADLINE + timedelta(minutes=5)


# ---------------------------------------------------------------- 冻结（T-14）
def test_freeze_is_immutable_idempotent_and_versions_chain(conn):
    (t1,) = freeze(conn, [draft()], at=BEFORE)
    assert freeze(conn, [draft()], at=BEFORE2) == [t1]                       # 相同内容重跑：no-op（T-14）
    rows = conn.execute("SELECT * FROM ticket").fetchall()
    assert len(rows) == 1 and rows[0]["frozen_at"].startswith(BEFORE.strftime("%Y-%m-%dT%H:%M"))     # 保留首次冻结时间
    (t2,) = freeze(conn, [draft(px="96")], at=BEFORE2, stage="preopen")       # 内容变化＝新版本，指向旧单
    assert t2 != t1
    assert conn.execute("SELECT supersedes FROM ticket WHERE ticket_id=?", (t2,)).fetchone()["supersedes"] == t1
    for sql in ("UPDATE ticket SET qty='1'", "DELETE FROM ticket"):
        with pytest.raises(sqlite3.DatabaseError, match="不可"):
            conn.execute(sql)


def test_t09_late_freeze_is_never_backfilled_as_formal_ticket(conn):
    (tid,) = freeze(conn, [draft()], at=AFTER)                                # 截止后才完成
    row = conn.execute("SELECT * FROM ticket WHERE ticket_id=?", (tid,)).fetchone()
    assert row["status"] == "missed_deadline" and row["action"] == SKIP and row["qty"] is None and "missed_deadline" in row["reason_json"]
    assert select_ticket(conn, cell(), deadline_at=DEADLINE).reason == "none_frozen"        # 不进入正式记录


def test_unavailable_when_key_data_missing_has_no_orders(conn):
    (tid,) = freeze(conn, [draft()], at=BEFORE, unavailable="account_snapshot_missing")
    row = conn.execute("SELECT * FROM ticket WHERE ticket_id=?", (tid,)).fetchone()
    assert row["status"] == "unavailable" and row["action"] == SKIP and row["qty"] is None
    assert select_ticket(conn, cell(), deadline_at=DEADLINE).ticket is None


def test_time_chain_and_kind_validation(conn):
    with pytest.raises(TicketError):
        freeze(conn, [draft()], at=BEFORE, stage="intraday")
    with pytest.raises(TicketError):
        freeze_tickets(conn, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=[draft()],
                       state_ref_type="line_state", state_ref="s", strategy_version="v", protocol_version="p",
                       generated_at=BEFORE + timedelta(hours=1), now=BEFORE, deadline_at=DEADLINE)         # 生成晚于冻结


# ---------------------------------------------------------------- T-27 唯一选择规则
def test_t27_selection_is_last_frozen_visible_before_deadline_and_late_versions_rejected(conn):
    freeze(conn, [draft(px="97")], at=BEFORE, stage="close")
    freeze(conn, [draft(px="96")], at=BEFORE2, stage="preopen")
    freeze(conn, [draft(px="95")], at=AFTER, stage="preopen")                 # 截止后：missed，不参与
    sel = select_ticket(conn, cell(), deadline_at=DEADLINE)
    assert sel.reason == "selected" and sel.ticket["limit_price"] == "96" and sel.ticket["stage"] == "preopen"


def test_selection_state_changed_invalidates_old_ticket_not_impersonating_new(conn):
    freeze(conn, [draft()], at=BEFORE, state_ref="state-A")
    assert select_ticket(conn, cell(), deadline_at=DEADLINE, current_state_ref="state-A").reason == "selected"
    sel = select_ticket(conn, cell(), deadline_at=DEADLINE, current_state_ref="state-B")      # 持仓/现金已变
    assert sel.ticket is None and sel.reason == "state_changed"
    # 盘前刷新失败（unavailable）：回退到较早的已冻结版本——前提是它仍有效
    freeze(conn, [draft()], at=BEFORE2, stage="preopen", state_ref="state-A", unavailable="data_stale")
    assert select_ticket(conn, cell(), deadline_at=DEADLINE, current_state_ref="state-A").reason == "selected"
    assert select_ticket(conn, cell(), deadline_at=DEADLINE, current_state_ref="state-B").ticket is None


def test_selection_cells_are_isolated_by_line_kind_and_code(conn):
    freeze(conn, [draft()], at=BEFORE)
    freeze(conn, [draft(px="90")], at=BEFORE, line="B1:ai_veto")
    freeze(conn, [draft(px="80")], at=BEFORE, kind="live_guidance", state_ref="acct-snap")
    assert select_ticket(conn, cell(), deadline_at=DEADLINE).ticket["limit_price"] == "97"
    assert select_ticket(conn, cell(line="B1:ai_veto"), deadline_at=DEADLINE).ticket["limit_price"] == "90"
    assert select_ticket(conn, cell(kind="live_guidance"), deadline_at=DEADLINE).ticket["state_ref_type"] == "account_snapshot"
    assert select_ticket(conn, cell(code="US.TSLA"), deadline_at=DEADLINE).reason == "not_in_latest_group"          # 该组里没有 TSLA


def test_f14_selection_is_group_atomic_no_stitching_of_old_and_new_and_expired_tickets_are_invalid(conn):
    freeze(conn, [draft(code="US.NVDA", px="97"), draft(code="US.TSLA", px="250", qty=5)], at=BEFORE)         # 旧组：A、B
    freeze(conn, [draft(code="US.NVDA", px="96")], at=BEFORE2, stage="preopen")                                 # 新组只刷新 A
    assert select_ticket(conn, cell("US.NVDA"), deadline_at=DEADLINE).ticket["limit_price"] == "96"
    sel = select_ticket(conn, cell("US.TSLA"), deadline_at=DEADLINE)
    assert sel.ticket is None and sel.reason == "not_in_latest_group"                                           # 不拼接「新 A + 旧 B」
    # 有效期：valid_to 早于截止的票失效
    freeze_tickets(conn, batch_id=B, line_id="B1:ai_x", kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=[draft(code="US.NVDA")],
                   state_ref_type="line_state", state_ref="s", strategy_version="v", protocol_version="p", generated_at=BEFORE, now=BEFORE, deadline_at=DEADLINE,
                   valid_to=BEFORE2 - timedelta(hours=1))
    assert select_ticket(conn, cell("US.NVDA", line="B1:ai_x"), deadline_at=DEADLINE).reason == "expired"


def test_f03_freeze_rejects_mismatched_kind_missing_or_rebuilt_predictions(conn):
    with pytest.raises(TicketError, match="不匹配"):
        freeze_tickets(conn, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=[draft()],
                       state_ref_type="account_snapshot", state_ref="s", strategy_version="v", protocol_version="p", generated_at=BEFORE, now=BEFORE, deadline_at=DEADLINE)
    with pytest.raises(TicketError, match="model_ref 不存在"):
        freeze(conn, [TicketDraft("US.NVDA", BUY, D("97"), 1, 1, None, ("x",), model_ref="ghost")], at=BEFORE)
    p = conn.execute("PRAGMA database_list").fetchone()["file"]
    seed_prediction(p, "p-rebuilt", "US.NVDA", TARGET.isoformat(), source_tag="rebuilt")
    with pytest.raises(TicketError, match="不是前向预测"):
        freeze(conn, [TicketDraft("US.NVDA", BUY, D("97"), 1, 1, None, ("x",), model_ref="p-rebuilt")], at=BEFORE)         # 重建预测不能冒充前向
    with pytest.raises(TicketError, match="不是前向预测|不符"):
        freeze(conn, [TicketDraft("US.TSLA", BUY, D("97"), 1, 1, None, ("x",), model_ref="p-US.NVDA")], at=BEFORE)         # 引用别的标的的预测
    with pytest.raises(TicketError, match="state_ref"):
        freeze(conn, [draft()], at=BEFORE, state_ref="")


def test_co02_coverage_counts_skips_and_misses(conn):
    freeze(conn, [draft(), draft(action=SKIP, px=None, qty=None, code="US.TSLA", reasons=("no_edge",))], at=BEFORE)
    cov = coverage(conn, B, L, "line_sim", "US", [TARGET, TARGET + timedelta(days=1)], ["US.NVDA", "US.TSLA"])
    assert cov["planned"] == 4 and cov["with_ticket"] == 2 and cov["missing"] == [("2026-03-06", "US.NVDA"), ("2026-03-06", "US.TSLA")]


# ---------------------------------------------------------------- 票据 → 引擎
def test_ticket_provider_feeds_engine_and_state_hash_guards_stale_tickets(conn):
    md = FakeMD()
    for d in cal.session_days("US", date(2026, 3, 2), date(2026, 3, 6)):
        md.set_day("US.NVDA", d, [(100, 101, 95, 99, 1_000_000)] * 6, close=99)
    init = state(10000)
    freeze(conn, [draft(px="97", qty=30)], at=BEFORE, state_ref=init.hash())
    prov = TicketProvider(conn, batch_id=B, line_id=L, market="US", codes=["US.NVDA"])
    run = run_line(md, market="US", currency="USD", initial=init, sessions=[TARGET], provider=prov, protocol=ExecProtocol(), fee_rules=FEES,
                   settlement=__import__("mystock2.ledger.settlement", fromlist=["SettlementRule"]).SettlementRule("US", 1))
    assert run.results[0].fills and run.results[0].fills[0].price == D("97") and run.results[0].positions == {"US.NVDA": D(30)}
    # 状态与冻结时不符：票据失效，无订单
    stale = TicketProvider(conn, batch_id=B, line_id=L, market="US", codes=["US.NVDA"])
    assert stale(state(9999), TARGET) == [] and stale.last_reasons[(TARGET, "US.NVDA")] == "state_changed"


# ---------------------------------------------------------------- 人类计划 / 暴露（T-13、T-35、T-39）
def human_state(cash=5000, held=0):
    st = LineState("USD", D(cash))
    if held:
        from mystock2.scoreboard.types import Lot
        st.lots["US.NVDA"] = [Lot(D(held), D(90), TARGET - timedelta(days=3))]
    return st


def rec(conn, *, action="BUY", px="97", qty=30, at=BEFORE, st=None, handling="truncate", code="US.NVDA"):
    st = st or human_state()
    return record_intent(conn, batch_id=B, line_id="B1:human_plan", market="US", code=code, target_session=TARGET, action=action,
                         limit_price=px, qty=qty, state=st, state_hash=st.hash(), now=at, deadline_at=DEADLINE, constraint_handling=handling,
                         rule=RULE, fee_rules=FEES)


def plan(conn, codes=("US.NVDA",)):
    return select_human_plan(conn, batch_id=B, line_id="B1:human_plan", market="US", target_session=TARGET, codes=list(codes), deadline_at=DEADLINE)


def test_t35_constraints_truncate_or_reject_and_deadline_rules(conn):
    r = rec(conn, qty=30, px="97", st=human_state(cash=1000))               # 现金只够 10 股（含费用）
    assert r.truncated and r.qty == 10 and any(n.startswith("qty_truncated_to_cash") for n in r.notes) and not r.late_record and not r.seen_ai
    with pytest.raises(IntentRejected, match="超预算"):
        rec(conn, qty=30, px="97", st=human_state(cash=1000), handling="reject")
    with pytest.raises(IntentRejected, match="超卖"):
        rec(conn, action="SELL", px="99", qty=50, st=human_state(cash=0, held=20), handling="reject")
    s = rec(conn, action="SELL", px="99", qty=50, st=human_state(cash=0, held=20))     # 截断到可卖库存
    assert s.qty == 20 and s.truncated
    with pytest.raises(IntentRejected):
        rec(conn, action="SELL", px="99", qty=5, st=human_state(cash=0, held=0))        # 没有库存
    with pytest.raises(IntentError):
        rec(conn, action="DANCE")
    with pytest.raises(IntentError):
        rec(conn, action="BUY", px=None, qty=None)
    assert rec(conn, action="NO_TRADE", px=None, qty=None).qty is None


def test_limit_price_rounding_or_rejection_to_legal_tick(conn):
    r = rec(conn, px="97.004", qty=10)
    assert any(n.startswith("limit_rounded") for n in r.notes)               # 买价向下取整到 0.01
    with pytest.raises(IntentRejected, match="合法价位"):
        rec(conn, px="97.004", qty=10, handling="reject")


def test_t13_t39_first_reveal_locks_last_pre_reveal_plan_and_later_edits_do_not_enter_human_plan(conn):
    first = rec(conn, px="97", qty=10, at=BEFORE)
    second = rec(conn, px="96", qty=10, at=BEFORE + timedelta(minutes=30))   # 揭示前改主意：取最后一个
    assert plan(conn)["US.NVDA"]["intent_id"] == second.intent_id and plan(conn)["US.NVDA"]["limit_price"] == "96"
    t_reveal = BEFORE + timedelta(hours=1)
    reveal(conn, batch_id=B, market="US", target_session=TARGET, channel="coach_show", version_hashes=["h1"], at=t_reveal)
    late = rec(conn, px="90", qty=20, at=t_reveal + timedelta(minutes=5))      # 看过 AI 之后修改
    assert late.seen_ai and late.late_record
    p = plan(conn)["US.NVDA"]
    assert p["intent_id"] == second.intent_id and p["limit_price"] == "96"     # 锁定揭示前版本；修改不进入 human_plan
    assert first.intent_id != second.intent_id
    assert first_reveal_at(conn, B, "US", TARGET).startswith(t_reveal.strftime("%Y-%m-%dT%H:%M"))
    with pytest.raises(sqlite3.DatabaseError, match="不可改写"):
        conn.execute("UPDATE intent SET seen_ai=0")
    with pytest.raises(sqlite3.DatabaseError, match="不可"):
        conn.execute("DELETE FROM intent_exposure")


def test_no_record_before_reveal_means_no_order_and_exposed_before_record_flag(conn):
    assert plan(conn)["US.NVDA"] == {"action": "NO_ORDER", "limit_price": None, "qty": None, "intent_id": None, "flags": ["plan_missing"], "valid_to": None, "state_hash": None}   # 缺失＝确定性无订单
    reveal(conn, batch_id=B, market="US", target_session=TARGET, channel="veto_export", version_hashes=["h"], at=BEFORE)
    rec(conn, px="97", qty=10, at=BEFORE + timedelta(minutes=10))              # 揭示之后才首次记录
    p = plan(conn)["US.NVDA"]
    assert p["action"] == "NO_ORDER" and p["flags"] == ["plan_missing", "exposed_before_record"]
    drafts = plan_to_drafts(plan(conn), {"US.NVDA": 1})
    assert drafts[0].action == SKIP and drafts[0].reason_codes == ("plan_missing", "exposed_before_record")


def test_late_after_deadline_record_is_not_formal(conn):
    r = rec(conn, px="97", qty=10, at=AFTER)
    assert r.late_record and not r.seen_ai
    assert plan(conn)["US.NVDA"]["action"] == "NO_ORDER"


def test_human_plan_provider_with_engine_and_hold_no_trade_variants(conn):
    md = FakeMD()
    for d in cal.session_days("US", date(2026, 3, 2), date(2026, 3, 6)):
        md.set_day("US.NVDA", d, [(100, 101, 95, 99, 1_000_000)] * 6, close=99)
    rec(conn, px="97", qty=10, at=BEFORE)
    prov = HumanPlanProvider(conn, batch_id=B, line_id="B1:human_plan", market="US", codes=["US.NVDA", "US.TSLA"])
    run = run_line(md, market="US", currency="USD", initial=state(5000), sessions=[TARGET], provider=prov, protocol=ExecProtocol(), fee_rules=FEES,
                   settlement=__import__("mystock2.ledger.settlement", fromlist=["SettlementRule"]).SettlementRule("US", 1))
    assert run.results[0].fills[0].price == D("97") and run.results[0].positions == {"US.NVDA": D(10)}
    assert prov.flags[(TARGET, "US.TSLA")] == ["plan_missing"]                    # 缺失计划：计入分母的无订单
    drafts = plan_to_drafts({"A": {"action": "HOLD", "limit_price": None, "qty": None, "intent_id": "i", "flags": []},
                             "B": {"action": "NO_TRADE", "limit_price": None, "qty": None, "intent_id": "j", "flags": []},
                             "C": {"action": "BUY", "limit_price": "10", "qty": "5", "intent_id": "k", "flags": []}}, {"C": 1})
    assert [d.action for d in drafts] == [HOLD, SKIP, BUY]


# ---------------------------------------------------------------- DbMarketData
def hourly_conn(tmp_path):
    p = tmp_path / "m.db"
    dbmod.migrate(p)
    return dbmod.connect_writer(p, "market")


def put_session_bars(conn, code, day, skip=None, market="US"):
    s = cal.session(market, day)
    bars, t = [], s.open_utc
    i = 0
    while t < s.close_utc:
        if market == "HK" and s.break_start_utc and t >= s.break_start_utc and t < s.break_end_utc:
            t = s.break_end_utc
            continue
        end = min(t + timedelta(hours=1), s.close_utc)
        if market == "HK" and s.break_start_utc and t < s.break_start_utc < end:
            end = s.break_start_utc
        if i != skip:
            bars.append(HourlyBar(code, t, end, "10", "11", "9", "10", "1000", True))
        t, i = end, i + 1
    put_hourly(conn, bars, source="syn")
    return len(bars)


def test_db_market_data_completeness_detects_missing_bar_and_allows_hk_lunch_break(tmp_path):
    c = hourly_conn(tmp_path)
    put_session_bars(c, "US.NVDA", date(2026, 3, 4))
    put_session_bars(c, "US.NVDA", date(2026, 3, 5), skip=2)                         # 缺一根
    put_session_bars(c, "HK.00700", date(2026, 3, 4), market="HK")                      # 午休间隙合法
    md = DbMarketData(c)
    assert md.session_complete("US.NVDA", date(2026, 3, 4)) and not md.session_complete("US.NVDA", date(2026, 3, 5))
    assert md.session_complete("HK.00700", date(2026, 3, 4))
    assert not md.session_complete("US.NVDA", date(2026, 3, 6))                           # 没有任何 bar
    assert [b.volume for b in md.hourly("US.NVDA", date(2026, 3, 4))][0] == 1000 and md.close("US.NVDA", date(2026, 3, 4)) is None


def test_db_market_data_close_uses_final_unadjusted_and_ignores_partial(tmp_path):
    from mystock2.market.bars import DailyBar, put_daily
    c = hourly_conn(tmp_path)
    put_daily(c, [DailyBar("US.NVDA", date(2026, 3, 4), "10", "11", "9", "10.5", "10.4", "1")], source="s", quality="ok")
    put_daily(c, [DailyBar("US.NVDA", date(2026, 3, 5), "10", "11", "9", "10.7", "10.6", "1")], source="s", quality="partial")
    md = DbMarketData(c)
    assert md.close("US.NVDA", date(2026, 3, 4)) == D("10.5")                              # 未复权 close，不是 adj_close
    assert md.close("US.NVDA", date(2026, 3, 5)) is None                                   # partial 不用
    _ = datetime(2026, 1, 1, tzinfo=UTC)


def test_reveal_without_any_record_still_flags_exposed_before_record(conn):
    reveal(conn, batch_id=B, market="US", target_session=TARGET, channel="veto_export", version_hashes=["h"], at=BEFORE)
    assert plan(conn)["US.NVDA"]["flags"] == ["plan_missing", "exposed_before_record"]     # 揭示后从未补录也要标记
