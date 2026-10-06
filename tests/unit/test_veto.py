import json
from datetime import date, datetime, timedelta, timezone

import pytest

from mystock2.assistant.veto import (
    FIELD_WHITELIST,
    VetoError,
    apply_adjustments,
    build_pack,
    import_veto,
    record_packet,
    validate_response,
)
from mystock2.coach.decide import BUY, SELL, SKIP, TicketDraft
from mystock2.coach.tickets import Cell, freeze_tickets, select_ticket
from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.market.bars import DailyBar  # noqa: F401
from tests.unit.coach_helpers import seed_prediction

UTC = timezone.utc
TARGET = date(2026, 3, 5)
DEADLINE = cal.project_deadline("US", TARGET)
B, L = "B1", "B1:ai_veto"
BEFORE = DEADLINE - timedelta(hours=10)
IMPORT_OK = DEADLINE - timedelta(hours=1)
AFTER = DEADLINE + timedelta(minutes=1)
CUTOFF = BEFORE


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "v.db"
    dbmod.migrate(p)
    seed_prediction(p, "p1", "US.NVDA", TARGET.isoformat())
    seed_prediction(p, "p2", "US.TSLA", TARGET.isoformat())
    return p


def conns(db):
    return dbmod.connect_writer(db, "assistant"), dbmod.connect_writer(db, "coach"), dbmod.connect_ro(db)


def base_tickets(coach, state_ref="s0"):
    drafts = [
        TicketDraft("US.NVDA", BUY, 97 * __import__("decimal").Decimal(1), 30, 1, None, ("edge_ok",), uncertainty={"n_train": 250, "width": "0.1", "c_rt": "0.002"}, model_ref="p1"),
        TicketDraft("US.TSLA", SELL, __import__("decimal").Decimal("250"), 10, 1, None, ("sell_target",), model_ref="p2"),
        TicketDraft("US.AMD", SKIP, reason_codes=("no_edge",)),
    ]
    freeze_tickets(coach, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=drafts, state_ref_type="line_state",
                   state_ref=state_ref, strategy_version="v", protocol_version="p", generated_at=BEFORE - timedelta(minutes=1), now=BEFORE, deadline_at=DEADLINE)


def rows(ro):
    return ro.execute("SELECT * FROM ticket WHERE line_id=? AND status='frozen' ORDER BY code, visible_at", (L,)).fetchall()


EVENTS = [{"evidence_id": "ev-1", "title": "公司公告：下周发布财报", "source": "IR", "published_at": (CUTOFF - timedelta(hours=3)).isoformat()}]
SUMMARY = [{"code": "US.NVDA", "holding": "no", "cash_pct": "0.9", "exposure_pct": "0.1"}]


def make_pack(ro, events=EVENTS):
    return build_pack(market="US", target_session=TARGET, base_tickets=rows(ro), line_state_summary=SUMMARY,
                      ohlcv={"US.NVDA": [{"date": "2026-03-04", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "100"}]},
                      events=events, input_cutoff_at=CUTOFF)


def resp(pack, **kw):
    base = {"pack_id": pack.pack_id, "verdict": "downgrade", "adjustments": [{"code": "US.NVDA", "type": "cancel_buy"}], "flags": ["earnings_within_2d"],
            "evidence_ids": ["ev-1"], "note": "财报临近"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# ---------------------------------------------------------------- 输入包
def test_pack_contains_only_whitelisted_fields_and_no_absolute_money_or_ids(db):
    a, c, ro = conns(db)
    base_tickets(c)
    pack = make_pack(ro)
    text = json.dumps(pack.content, ensure_ascii=False)
    for forbidden in ("account", "acc_id", "客户", "deal_id", "batch_id", "budget", "equity", "cash\":"):
        assert forbidden not in text
    assert set(pack.content) == {"pack_version", "market", "target_session", "tickets", "line_state", "ohlcv", "events"}
    assert set(pack.content["line_state"][0]) == {"code", "holding", "cash_pct", "exposure_pct"}              # 只有粗粒度比例与是否持有
    assert "base_hash" in pack.content["tickets"][0] and set(pack.base_hashes) == {"US.NVDA", "US.TSLA", "US.AMD"}
    assert "严格 JSON" in pack.markdown and pack.pack_id in pack.markdown and "不得" in pack.markdown
    assert any(f.startswith("tickets[]") for f in FIELD_WHITELIST) and "account" not in "".join(FIELD_WHITELIST)
    assert make_pack(ro).pack_id == pack.pack_id                                                                     # 内容哈希稳定


def test_events_published_after_cutoff_are_rejected_as_lookahead(db):
    a, c, ro = conns(db)
    base_tickets(c)
    late = [{"evidence_id": "ev-9", "title": "x", "source": "s", "published_at": (CUTOFF + timedelta(minutes=1)).isoformat()}]
    with pytest.raises(VetoError, match="前视"):
        make_pack(ro, late)


# ---------------------------------------------------------------- 校验（T-11）
def check(ro, pack, **kw):
    return validate_response(resp(pack, **kw), pack.content, pack.pack_id)


def test_t11_all_privilege_escalations_are_rejected(db):
    a, c, ro = conns(db)
    base_tickets(c)
    pack = make_pack(ro)
    ok, errs = check(ro, pack)
    assert ok is not None and errs == []
    cases = {
        "放大数量": dict(adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 60}], verdict="downgrade"),
        "数量不变": dict(adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 30}]),
        "取消卖单": dict(adjustments=[{"code": "US.TSLA", "type": "cancel_buy"}]),
        "缩小卖单": dict(adjustments=[{"code": "US.TSLA", "type": "reduce_buy_qty", "qty": 5}]),
        "动 SKIP 单": dict(adjustments=[{"code": "US.AMD", "type": "cancel_buy"}]),
        "新增操作单": dict(adjustments=[{"code": "US.NEW", "type": "add_buy", "qty": 10}]),
        "改限价": dict(adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 10, "limit_price": "99"}]),
        "未知标的": dict(adjustments=[{"code": "US.XYZ", "type": "cancel_buy"}]),
        "非整数数量": dict(adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 2.5}]),
        "布尔数量": dict(adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": True}]),
        "取消带数量": dict(adjustments=[{"code": "US.NVDA", "type": "cancel_buy", "qty": 1}]),
        "放行却带调整": dict(verdict="allow"),
        "降级无调整": dict(adjustments=[]),
        "证据不在包内": dict(evidence_ids=["ev-404"]),
        "有标签无证据": dict(evidence_ids=[]),
        "错误标签": dict(flags=["Bad Flag!"]),
        "标签过多": dict(flags=[f"f{i}" for i in range(11)]),
        "备注过长": dict(note="x" * 201),
        "错包": dict(pack_id="other"),
        "重复调整": dict(adjustments=[{"code": "US.NVDA", "type": "cancel_buy"}, {"code": "US.NVDA", "type": "cancel_buy"}]),
    }
    for name, kw in cases.items():
        parsed, errors = check(ro, pack, **kw)
        assert parsed is None and errors, f"越权未被拒绝：{name}"
    parsed, errors = validate_response(json.dumps({**json.loads(resp(pack)), "new_orders": [{"code": "US.X", "qty": 5}]}), pack.content, pack.pack_id)
    assert parsed is None and any(e.startswith("unknown_keys") for e in errors)
    for bad in ("not json", "[]", '{"verdict": NaN}', ""):
        parsed, errors = validate_response(bad, pack.content, pack.pack_id)
        assert parsed is None and errors


def test_allowed_actions_cancel_reduce_and_flags_only(db):
    a, c, ro = conns(db)
    base_tickets(c)
    pack = make_pack(ro)
    reduce = json.loads(resp(pack, adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 10}]))
    parsed, errors = validate_response(json.dumps(reduce), pack.content, pack.pack_id)
    assert errors == []
    drafts = {d.code: d for d in apply_adjustments(rows(ro), parsed)}
    assert drafts["US.NVDA"].qty == 10 and "llm_veto_reduce" in drafts["US.NVDA"].reason_codes and drafts["US.NVDA"].limit_price == 97   # 限价不变，只更保守
    assert drafts["US.TSLA"].action == SELL and drafts["US.TSLA"].qty == 10                                                              # 卖单不受影响
    assert all("llm_flag:earnings_within_2d" in d.reason_codes for d in drafts.values())                                                  # 标签可附在任意操作单上
    flag_only = json.loads(resp(pack, verdict="allow", adjustments=[]))
    parsed, errors = validate_response(json.dumps(flag_only), pack.content, pack.pack_id)
    assert errors == []


# ---------------------------------------------------------------- 导入（T-12、T-30）
def do_import(db, pack, text, now=IMPORT_OK, pack_id=None):
    _a, _c, ro = conns(db)
    return import_veto(dbmod.connect_writer(db, "veto"), ro, pack_id=pack_id or pack.pack_id, response_text=text, provider="manual", model_id="model-x", prompt_version="v1", now=now,
                       deadline_at=DEADLINE, strategy_version="v", protocol_version="p", state_ref="s0")


def setup_pack(db):
    a, c, ro = conns(db)
    base_tickets(c)
    pack = make_pack(ro)
    record_packet(a, pack, batch_id=B, line_id=L, exported_at=CUTOFF)
    return pack


def selected(db, code="US.NVDA"):
    return select_ticket(dbmod.connect_ro(db), Cell(B, L, "line_sim", "US", TARGET.isoformat(), code), deadline_at=DEADLINE, current_state_ref="s0").ticket


def test_applied_veto_appends_new_version_selected_by_rule_and_logs_call(db):
    pack = setup_pack(db)
    res = do_import(db, pack, resp(pack))
    assert res.status == "applied" and len(res.tickets) == 3
    t = selected(db)
    assert t["action"] == SKIP and "llm_veto_cancel" in t["reason_json"] and t["supersedes"] is not None          # 新版本指向旧单，旧单保留
    assert selected(db, "US.TSLA")["action"] == SELL                                                                # 卖单照旧
    ro = dbmod.connect_ro(db)
    call = ro.execute("SELECT * FROM llm_call").fetchone()
    assert (call["status"], call["provider"], call["model_id"], call["prompt_version"]) == ("applied", "manual", "model-x", "v1") and call["input_hash"]
    assert ro.execute("SELECT COUNT(*) c FROM ticket WHERE code='US.NVDA'").fetchone()["c"] == 2                     # 机械单仍在（可对比 ai 与 ai_veto）


def test_t30_wrong_pack_replay_late_and_updated_base_are_rejected_and_mechanical_stands(db):
    pack = setup_pack(db)
    with pytest.raises(VetoError, match="未知输入包"):
        do_import(db, pack, resp(pack), pack_id="nope")                                                             # 错包
    assert do_import(db, pack, resp(pack), now=AFTER).reason == "late_after_deadline"                                # 以导入完成时间判截止
    assert selected(db)["action"] == BUY                                                                            # 机械单照常
    assert do_import(db, pack, resp(pack)).status == "applied"
    assert do_import(db, pack, resp(pack)).reason == "replay"                                                       # 重放被拒
    ro = dbmod.connect_ro(db)
    assert sorted(r["status"] for r in ro.execute("SELECT status FROM llm_call")) == ["applied", "rejected", "rejected"]


def test_base_ticket_updated_invalidates_old_pack(db):
    pack = setup_pack(db)
    a, c, ro = conns(db)
    freeze_tickets(c, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="preopen",
                   drafts=[TicketDraft("US.NVDA", BUY, 96 * __import__("decimal").Decimal(1), 30, 1, None, ("edge_ok",))], state_ref_type="line_state", state_ref="s0",
                   strategy_version="v", protocol_version="p", generated_at=IMPORT_OK - timedelta(minutes=2), now=IMPORT_OK - timedelta(minutes=1), deadline_at=DEADLINE)
    res = do_import(db, pack, resp(pack))
    assert res.status == "rejected" and res.reason == "base_ticket_updated"                                         # 基础单更新，旧包失效 → 按「无否决」处理
    assert selected(db)["limit_price"] == "96"


def test_t12_non_json_or_invalid_closes_veto_layer_and_leaves_mechanical_orders(db):
    pack = setup_pack(db)
    res = do_import(db, pack, "抱歉我无法完成这个任务")
    assert res.status == "invalid" and res.tickets == [] and res.reason.startswith("not_json")
    assert selected(db)["action"] == BUY and selected(db)["qty"] == "30"
    bad = do_import(db, pack, resp(pack, adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 60}]))
    assert bad.status == "rejected" and "qty_not_smaller" in bad.reason and selected(db)["qty"] == "30"
    assert dbmod.connect_ro(db).execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"] == 3                       # 没有任何新票


def test_allow_without_changes_logs_but_creates_no_tickets(db):
    pack = setup_pack(db)
    res = do_import(db, pack, json.dumps({"pack_id": pack.pack_id, "verdict": "allow", "adjustments": [], "flags": [], "evidence_ids": [], "note": "ok"}))
    assert res.status == "applied" and res.tickets == [] and dbmod.connect_ro(db).execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"] == 3


def test_assistant_cannot_touch_ledger_tables_or_tickets_directly(db):
    a, c, ro = conns(db)
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        a.execute("INSERT INTO ledger_event(event_id) VALUES ('x')")
    with pytest.raises(sqlite3.DatabaseError):
        a.execute("DELETE FROM ticket")
    _ = datetime(2026, 1, 1, tzinfo=UTC)


def test_veto_precision_counts_would_be_losing_buys_and_reports_insufficient_sample(db):
    from datetime import timedelta as td
    from decimal import Decimal as Dc

    from mystock2.ledger.fees import FeeRule
    from mystock2.scoreboard.types import ExecProtocol
    from mystock2.scoreboard.veto_stats import MIN_SAMPLE, veto_precision
    from tests.unit.scoreboard_helpers import FakeMD

    md = FakeMD()
    days = cal.session_days("US", date(2026, 3, 2), date(2026, 3, 20))
    for d in days:
        md.set_day("US.NVDA", d, [(100, 101, 90, 95, 1_000_000)] * 6, close=95)          # 限价 97 > low 90：被否决的买单本会成交
    md.set_day("US.NVDA", cal.next_session("US", cal.next_session("US", cal.next_session("US", cal.next_session("US", cal.next_session("US", TARGET))))),
               [(100, 101, 90, 99, 1_000_000)] * 6, close=99)                             # 5 日后收盘 99 > 97：不亏
    a, c, ro = conns(db)
    base_tickets(c)
    pack = make_pack(ro)
    record_packet(a, pack, batch_id=B, line_id=L, exported_at=CUTOFF)
    do_import(db, pack, resp(pack))
    rules = [FeeRule("s", "US", "ANY", "order", "USD", pct_fee=Dc("0.001"), min_fee=Dc("1"))]
    res = veto_precision(dbmod.connect_ro(db), md, market="US", fee_rules=rules, protocol=ExecProtocol())
    assert (res.vetoed_buys, res.would_fill, res.would_lose) == (1, 1, 0)                 # 本会成交但不亏（97 → 99）
    assert res.precision is None and "不足" in res.note and MIN_SAMPLE == 5                # 样本不足：不给比例
    _ = td


def test_f23_pack_builder_rejects_fields_outside_the_whitelist(db):
    a, c, ro = conns(db)
    base_tickets(c)
    bad_state = [{"code": "US.NVDA", "holding": "no", "cash_pct": "0.9", "exposure_pct": "0.1", "account_id": "A1", "cash": "123456.78"}]
    with pytest.raises(VetoError, match="白名单"):
        build_pack(market="US", target_session=TARGET, base_tickets=rows(ro), line_state_summary=bad_state, ohlcv={}, events=[], input_cutoff_at=CUTOFF)
    bad_bar = {"US.NVDA": [{"date": "2026-03-04", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "1", "equity": "9"}]}
    with pytest.raises(VetoError, match="白名单"):
        build_pack(market="US", target_session=TARGET, base_tickets=rows(ro), line_state_summary=SUMMARY, ohlcv=bad_bar, events=[], input_cutoff_at=CUTOFF)


def test_f15_state_changed_between_export_and_import_rejects_instead_of_rebinding(db):
    pack = setup_pack(db)
    a, c, ro = conns(db)
    res = import_veto(dbmod.connect_writer(db, "veto"), ro, pack_id=pack.pack_id, response_text=resp(pack), provider="manual", model_id="m", prompt_version="v1",
                      now=IMPORT_OK, deadline_at=DEADLINE, strategy_version="v", protocol_version="p", state_ref="DIFFERENT-STATE")
    assert res.status == "rejected" and res.reason == "state_changed"                       # 不把旧数量绑定到新状态
    assert dbmod.connect_ro(db).execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"] == 3


def test_f15_freeze_and_receipt_commit_atomically(db, monkeypatch):
    import mystock2.assistant.veto as v
    pack = setup_pack(db)

    def boom(*a, **k):
        raise RuntimeError("日志写入失败")
    monkeypatch.setattr(v, "_log", boom)
    with pytest.raises(RuntimeError):
        do_import(db, pack, resp(pack))
    ro = dbmod.connect_ro(db)
    assert ro.execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"] == 3                  # 回执写失败 → 新票也一并回滚，不会出现「票已生效、回执缺失」
    assert ro.execute("SELECT COUNT(*) c FROM llm_call").fetchone()["c"] == 0
    monkeypatch.undo()
    assert do_import(db, pack, resp(pack)).status == "applied"                              # 可重试


def test_p0_3_partial_veto_keeps_untouched_sell_and_skip_in_the_selected_group(db):
    """审核 P0-3：否决只取消一张买单（flags 为空，其余单内容不变）时，未改的卖单与 SKIP 必须仍被选中——
    否则等于 LLM 取消了卖单。冻结组显式登记全部成员（含沿用的旧行）。"""
    pack = setup_pack(db)
    res = do_import(db, pack, resp(pack, flags=[]))
    assert res.status == "applied"
    assert selected(db, "US.NVDA")["action"] == SKIP
    assert selected(db, "US.TSLA")["action"] == SELL
    assert selected(db, "US.AMD")["action"] == SKIP
    ro = dbmod.connect_ro(db)
    groups = ro.execute("SELECT member_ids FROM ticket_group WHERE line_id=? ORDER BY frozen_at", (L,)).fetchall()
    assert len(groups) == 2 and len(json.loads(groups[-1]["member_ids"])) == 3


def test_partial_refresh_and_identical_rerun_follow_the_explicit_group(db):
    """部分刷新：只有一张单内容变了也登记整组；与最新组完全相同的重跑是 no-op（保留首次冻结时间）。"""
    _a, c, _ro = conns(db)
    base_tickets(c)
    base_tickets(c)                                                               # 同内容重跑：不新增组
    ro = dbmod.connect_ro(db)
    assert ro.execute("SELECT COUNT(*) n FROM ticket_group").fetchone()["n"] == 1
    drafts = [TicketDraft("US.NVDA", SKIP, reason_codes=("no_edge",), model_ref="p1"),
              TicketDraft("US.TSLA", SELL, __import__("decimal").Decimal("250"), 10, 1, None, ("sell_target",), model_ref="p2"),
              TicketDraft("US.AMD", SKIP, reason_codes=("no_edge",))]
    freeze_tickets(c, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=drafts, state_ref_type="line_state",
                   state_ref="s0", strategy_version="v", protocol_version="p", generated_at=BEFORE, now=BEFORE + timedelta(minutes=5), deadline_at=DEADLINE)
    assert selected(db, "US.NVDA")["action"] == SKIP and selected(db, "US.TSLA")["action"] == SELL and selected(db, "US.AMD")["action"] == SKIP
    # 截止前看得到的是最后一个组；把截止设在第二组之前，选到的是第一组的买单
    early = select_ticket(dbmod.connect_ro(db), Cell(B, L, "line_sim", "US", TARGET.isoformat(), "US.NVDA"), deadline_at=BEFORE + timedelta(minutes=1),
                          current_state_ref="s0").ticket
    assert early["action"] == BUY


def test_veto_reduction_must_stay_on_whole_lots(db):
    """变异 M2：每手 100 股的买单，否决只能减到整手（150 股被拒，100 股可以）；测试里每手都是 1 股时这条规则从未被触发。"""
    _a, c, ro = conns(db)
    d = TicketDraft("US.NVDA", BUY, __import__("decimal").Decimal("97"), 300, 100, None, ("edge_ok",), uncertainty={"n_train": 250}, model_ref="p1")
    freeze_tickets(c, batch_id=B, line_id=L, kind="line_sim", market="US", target_session=TARGET, stage="close", drafts=[d], state_ref_type="line_state",
                   state_ref="s0", strategy_version="v", protocol_version="p", generated_at=BEFORE - timedelta(minutes=1), now=BEFORE, deadline_at=DEADLINE)
    pack = make_pack(ro)
    _, errors = check(ro, pack, adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 150}])
    assert errors == ["qty_not_lot:US.NVDA"]
    ok, errors = check(ro, pack, adjustments=[{"code": "US.NVDA", "type": "reduce_buy_qty", "qty": 100}])
    assert ok is not None and errors == []
