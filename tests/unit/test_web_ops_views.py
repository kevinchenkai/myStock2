"""M3b：操作单（密封语义）、记分牌、复盘、数据状态四个视图 + 持仓「当前操作单」列的业务口径（合成数据）。"""
import json
from decimal import Decimal as D

import pytest

from mystock2.core import db as dbmod
from mystock2.core.timeutil import ensure_utc

from .test_web_fixtures import build_demo_db, get_view, make_app
from .test_web_opsdata import (
    BATCH,
    HUMAN_LIMIT,
    LEAK_KEYS,
    LEAK_VALUES,
    LEAK_WORDS,
    OLD_LIMIT,
    OLD_QTY,
    OLD_TARGET,
    SENT_LIMIT,
    SENT_PRED_HIGH,
    SENT_PRED_LOW,
    SENT_QTY,
    SENT_Y_HIGH,
    SENT_Y_LOW,
    TARGET,
    add_reveal,
    build_ops_db,
    seed_batch,
    seed_late_ticket,
    seed_protocol,
    seed_run,
)

pytestmark = pytest.mark.filterwarnings("ignore")

EXTRA_LEAK = ["77.77", "4747", "live_guidance_sentinel", "late_ticket_0001", "999.99", "9999", "124.5", "555", "69097.5", "69,097.50"]


def client_for(tmp_path, **kw):
    db = build_ops_db(tmp_path, **kw)
    return db, make_app(tmp_path, db).test_client()


def raw(client, view, **params):
    r = client.get(f"/api/v/{view}", query_string=params)
    assert r.status_code == 200, r.data[:300]
    return r.get_data(as_text=True)


def assert_no_sealed_content(text, keys=True):
    """密封时响应（JSON 全文）里不得出现 AI 单的任何值、字段名或能反推动作的词。"""
    for v in [*LEAK_VALUES, *EXTRA_LEAK]:
        assert v not in text, f"泄露了密封内容：{v}"
    for k in LEAK_KEYS if keys else []:
        assert k not in text, f"出现了内容字段名：{k}"
    for w in LEAK_WORDS:
        assert w not in text, f"出现了能反推动作的词：{w}"


def mkt(body, market="US"):
    return next(m for m in body["data"]["markets"] if m["market"] == market)


# ====================================================================== 操作单：密封
def test_sealed_when_no_exposure_returns_state_and_counts_only_for_every_param_combination(tmp_path):
    db, c = client_for(tmp_path)
    combos = [{}, {"market": "US"}, {"batch": BATCH}, {"target": TARGET}, {"target": OLD_TARGET}, {"batch": BATCH, "market": "US", "target": TARGET},
              {"target": "2026-03-04", "market": "US"}]
    for q in combos:
        text = raw(c, "tickets", **q)
        assert_no_sealed_content(text)
        body = json.loads(text)
        assert body["status"] == "ok"
        for m in body["data"]["markets"]:
            assert m["state"] == "sealed" and m["state_text"] == "已密封" and m["reveal"] is None and m["rows"] == []
    m = mkt(get_view(c, "tickets")[1])
    assert m["target_session"] == TARGET and ensure_utc(m["deadline_at"]) == ensure_utc("2026-03-11T13:00:00Z")   # 09:00 ET（美东夏令时）
    cnt = m["counts"]
    assert cnt["tickets"] == 5 and cnt["cells"] == 3 and cnt["by_stage"] == {"close": 3, "preopen": 2}
    assert cnt["by_status"] == {"frozen": 4, "missed_deadline": 0, "unavailable": 1}
    assert cnt["coverage"] == {"planned_cells": 3, "cells_with_ticket": 3, "cells_with_frozen": 2}      # CO-02：只给计数
    assert set(m) == {"market", "target_session", "deadline_at", "state", "state_text", "reveal", "counts", "rows"}   # 没有任何内容字段
    # 密封也不等于「看不到有几个单」：页面上能核对张数/阶段/状态
    assert get_view(c, "tickets", target=OLD_TARGET)[1]["data"]["markets"][0]["counts"]["by_status"]["missed_deadline"] == 1


def test_sealed_response_has_no_ticket_hash_or_state_ref_and_header_does_not_leak(tmp_path):
    db, c = client_for(tmp_path)
    code, body = get_view(c, "tickets")
    ro = dbmod.connect_ro(db)
    for r in ro.execute("SELECT frozen_hash, ticket_id, state_ref FROM ticket"):
        for v in (r["frozen_hash"], r["ticket_id"]):
            assert v not in json.dumps(body, ensure_ascii=False)
    assert "STATE_REF_AAA" not in json.dumps(body)                       # 状态哈希在揭示前同样不返回
    assert body["header"]["staleness"]["label"] in ("新鲜", "陈旧", "未知")


def test_exposure_record_reveals_selected_ticket_with_all_fields(tmp_path):
    db, c = client_for(tmp_path, reveal_target=True)
    code, body = get_view(c, "tickets")
    m = mkt(body)
    assert m["state"] == "revealed" and m["reveal"]["count"] == 1 and m["reveal"]["channels"] == ["coach_show"]
    rows = {r["code"]: r for r in m["rows"]}
    nv = rows["US.NVDA"]
    # 多版本：close 版本（123.45×777）与 preopen 版本（124.50×555）；唯一选择规则取截止前最后一个已冻结版本
    assert nv["action_code"] == "BUY" and nv["limit_price"]["text"] == "124.50 USD" and nv["qty"]["v"] == "555" and nv["stage"] == "preopen"
    assert nv["effective"]["code"] == "selected"
    assert nv["reasons"] == ["edge_ok", "buy_target"] and nv["uncertainty"]["width"] == "0.07771"
    assert nv["reserved_cash"]["text"] == "69,097.50 USD" and ensure_utc(nv["deadline_at"]) == ensure_utc("2026-03-11T13:00:00Z")
    assert nv["state_ref"] == "STATE_REF_AAA" and len(nv["frozen_hash"]) == 64 and nv["version_count"] == 2
    sel = [v for v in nv["versions"] if v["selected"]]
    assert len(sel) == 1 and sel[0]["stage"] == "preopen" and sel[0]["qty"]["v"] == "555"
    assert any(v["limit_price"]["v"] == SENT_LIMIT and not v["selected"] for v in nv["versions"])         # 旧版本列出但不采用
    assert rows["US.TSLA"]["action_code"] == "HOLD"
    assert rows["US.AAPL"]["effective"]["code"] == "not_in_latest_group" and rows["US.AAPL"]["status"] == "unavailable"   # CO-02：缺数据的单显示出来；最新一组（盘前）没有它
    assert "生效" not in rows["US.AAPL"]["effective"]["text"]
    # 价格带币种、红涨绿跌不适用于限价（中性）
    assert nv["limit_price"].get("dir") is None and nv["limit_price"]["ccy"] == "USD"


def test_reveal_is_per_batch_market_and_target_not_global(tmp_path):
    db, c = client_for(tmp_path)
    w = dbmod.connect_writer(db, "coach")
    from datetime import date

    from mystock2.coach.intents import reveal
    reveal(w, batch_id="OTHER_BATCH", market="US", target_session=date.fromisoformat(TARGET), channel="coach_show", version_hashes=["x"], at="2026-03-11T05:45:00Z")
    reveal(w, batch_id=BATCH, market="HK", target_session=date.fromisoformat(TARGET), channel="coach_show", version_hashes=["x"], at="2026-03-11T05:45:00Z")
    reveal(w, batch_id=BATCH, market="US", target_session=date.fromisoformat(OLD_TARGET), channel="coach_show", version_hashes=["x"], at="2026-03-11T05:45:00Z")
    w.close()
    text = raw(c, "tickets")                                             # 别的批次/市场/目标日的揭示都不能解封这一格
    assert mkt(json.loads(text))["state"] == "sealed"
    assert_no_sealed_content(text)
    old = mkt(get_view(c, "tickets", target=OLD_TARGET)[1])              # 旧目标日确实已揭示
    assert old["state"] == "revealed" and next(r for r in old["rows"] if r["code"] == "US.NVDA")["limit_price"]["v"] == OLD_LIMIT
    assert any(r["status"] == "missed_deadline" for r in old["rows"])    # 错过截止的行（CO-02）
    # 新目标日仍密封
    assert mkt(get_view(c, "tickets", target=TARGET)[1])["state"] == "sealed"


def test_reveal_dated_in_the_future_stays_sealed(tmp_path):
    db, c = client_for(tmp_path)
    add_reveal(db, at="2026-03-12T00:00:00Z")                              # 晚于演示时钟 NOW：保守视为未揭示
    text = raw(c, "tickets")
    assert mkt(json.loads(text))["state"] == "sealed"
    assert_no_sealed_content(text)


def test_late_visible_version_after_deadline_is_not_selected(tmp_path):
    db, c = client_for(tmp_path, reveal_target=True)
    seed_late_ticket(db)                                                    # visible_at 14:00Z > 13:00Z 截止
    nv = next(r for r in mkt(get_view(c, "tickets")[1])["rows"] if r["code"] == "US.NVDA")
    assert nv["limit_price"]["v"] == "124.5" and nv["qty"]["v"] == "555"      # 仍是截止前最后一个版本
    late = [v for v in nv["versions"] if v["after_deadline"]]
    assert len(late) == 1 and late[0]["qty"]["v"] == "9999" and late[0]["selected"] is False
    assert nv["version_count"] == 3


def test_changed_state_hash_invalidates_the_ticket(tmp_path):
    db = build_ops_db(tmp_path, run=False, reveal_target=True)
    seed_run(db, ai_state_hash="SOMETHING_ELSE")                            # 记分牌 run 里该线目标日的状态哈希已不是单上的 state_ref
    c = make_app(tmp_path, db).test_client()
    nv = next(r for r in mkt(get_view(c, "tickets")[1])["rows"] if r["code"] == "US.NVDA")
    assert nv["effective"]["code"] == "state_changed" and "失效" in nv["effective"]["text"] and nv["action"]["tag"] == "已失效"
    assert nv["current_state_hash"] == "SOMETHING_ELSE" and nv["state_ref"] == "STATE_REF_AAA"


def test_matching_state_hash_keeps_the_ticket_effective_and_missing_run_is_unchecked(tmp_path):
    db = build_ops_db(tmp_path, reveal_target=True)
    nv = next(r for r in mkt(get_view(make_app(tmp_path, db).test_client(), "tickets")[1])["rows"] if r["code"] == "US.NVDA")
    assert nv["effective"]["code"] == "selected" and nv["current_state_hash"] == "STATE_REF_AAA"
    sub = tmp_path / "x"
    sub.mkdir()
    db2 = build_ops_db(sub, reveal_target=True, run=False)
    nv2 = next(r for r in mkt(get_view(make_app(sub, db2).test_client(), "tickets")[1])["rows"] if r["code"] == "US.NVDA")
    assert nv2["effective"]["code"] == "unchecked" and "未能校验" in nv2["effective"]["text"]


def test_human_lines_and_live_guidance_are_never_shown_even_when_revealed(tmp_path):
    db, c = client_for(tmp_path, reveal_target=True)
    text = raw(c, "tickets")
    for v in (HUMAN_LIMIT, "31337", "77.77", "4747", "live_guidance_sentinel"):
        assert v not in text
    body = json.loads(text)
    assert all("human" not in r["line_id"] for m in body["data"]["markets"] for r in m["rows"])
    lg = body["data"]["live_guidance"]
    assert lg["status"] == "未提供" and lg["text"]["text"] == "未提供"                       # live_guidance 占位
    assert body["data"]["markets"][0]["counts"]["tickets"] == 5                              # 人类线单与 live_guidance 单不计入


def test_tickets_unavailable_without_batch_and_ok_for_batch_without_tickets(tmp_path):
    p = tmp_path / "e.db"
    dbmod.migrate(p)
    code, b = get_view(make_app(tmp_path, p).test_client(), "tickets")
    assert b["status"] == "unavailable" and b["error"]["code"] == "no_batch"
    sub = tmp_path / "y"
    sub.mkdir()
    db = build_demo_db(sub)
    seed_batch(db)
    c = make_app(sub, db).test_client()
    code, b = get_view(c, "tickets")
    assert b["status"] == "ok" and b["data"]["markets"] == [] and any("还没有任何 AI 单" in n for n in b["header"]["notes"])
    assert get_view(c, "tickets", batch="nope")[1]["error"]["code"] == "batch_not_found"
    assert get_view(c, "tickets", target="2026/03/11")[1]["error"]["code"] == "bad_target"


# ====================================================================== 持仓「当前操作单」列
def test_holdings_order_column_shows_only_state_never_the_action(tmp_path):
    db, c = client_for(tmp_path)
    text = raw(c, "holdings")
    assert_no_sealed_content(text, keys=False)               # 持仓行本身有「qty」（账本数量）这个键，不是操作单字段
    rows = {r["code"]: r for r in json.loads(text)["data"]["rows"]}
    assert rows["US.NVDA"]["order"]["text"] == f"已密封（目标日 {TARGET}）"
    assert rows["HK.00700"]["order"]["text"] == "无已冻结的 AI 单"                          # 港股没有单
    add_reveal(db)
    text = raw(c, "holdings")
    rows = {r["code"]: r for r in json.loads(text)["data"]["rows"]}
    assert rows["US.NVDA"]["order"]["text"] == f"已揭示（目标日 {TARGET}）"
    for v in (SENT_LIMIT, SENT_QTY, "124.5", "555", "BUY", "买入"):                       # 即使已揭示，持仓页也只显示状态
        assert v not in text


# ====================================================================== 记分牌
def sb(client, **q):
    code, body = get_view(client, "scoreboard", **q)
    assert code == 200 and body["status"] == "ok", body
    return body


def line(body, kind, part="formal"):
    return next(x for x in body["data"][part]["lines"] if x["kind"] == kind)


def test_scoreboard_header_banner_and_three_evidence_partitions(tmp_path):
    db, c = client_for(tmp_path)
    b = sb(c)
    d = b["data"]
    assert "模拟线结论不等于按指导操作的结论" in d["banner"] and any("模拟线结论不等于按指导操作的结论" in n for n in b["header"]["notes"])
    assert [x["kind"] for x in d["formal"]["lines"]] == ["ai", "buyhold", "human_plan"]
    assert [x["kind"] for x in d["descriptive"]["lines"]] == ["human_actual"]                   # human_actual 单独分区，描述性
    assert "描述性" in d["descriptive"]["title"] and "不进入正式比较" in d["descriptive"]["note"]
    assert d["live_guidance"]["status"] == "未提供"
    text = json.dumps(d, ensure_ascii=False)
    assert "打败" not in text and "win_rate" not in text    # 不合成单一结论
    assert d["currency"] == "USD" and d["e0"]["text"] == "10,000.00 USD" and d["market"] == "US"


def test_scoreboard_metrics_use_scoreboard_module_and_decimal(tmp_path):
    db, c = client_for(tmp_path)
    m = line(sb(c), "ai")["metrics"]
    assert m["cumulative_return"]["v"] == "0.015" and m["cumulative_return"]["text"] == "+1.50%" and m["cumulative_return"]["dir"] == "up"   # R(T)=(E_T−E0)/E0
    assert m["max_drawdown"]["text"] == "0.49%" and m["max_drawdown"].get("dir") is None                  # (10200−10150)/10200
    assert D(m["max_drawdown"]["v"]) == D(50) / D(10200)
    assert m["coverage"]["text"] == "42.86%" and (m["days_ok"], m["days_planned"], m["unknown_days"], m["paused_days"]) == (3, 7, 1, 3)
    assert m["ambiguous_days"] == 1 and m["fills"] == 1 and m["fees_cum"]["text"] == "1.00 USD"
    assert m["turnover"]["text"] == "9.85%"                                                               # 成交额 1000 / 平均权益 10150
    assert m["last_ok_date"] == "2026-03-04"
    hp = line(sb(c), "human_plan")["metrics"]
    assert hp["cumulative_return"]["v"] == "0.045" and hp["coverage"]["text"] == "100.00%" and hp["unknown_days"] == 0


def test_unknown_and_paused_days_break_the_equity_curve_and_are_annotated(tmp_path):
    db, c = client_for(tmp_path)
    ch = sb(c)["data"]["formal"]["chart"]
    ai = next(s for s in ch["series"] if s["kind"] == "ai")
    assert ch["dates"][0] == "2026-03-02" and len(ch["dates"]) == 7
    assert ai["points"] == ["10100", "10200", "10150", None, None, None, None]                            # 不插值、不记零
    assert all(isinstance(p, str) or p is None for p in ai["points"])
    assert ch["gaps"]["2026-03-05"].endswith("UNKNOWN") and ch["gaps"]["2026-03-06"].endswith("PAUSED")
    assert "2026-03-04" not in ch["gaps"]
    hp = next(s for s in ch["series"] if s["kind"] == "human_plan")
    assert None not in hp["points"]
    assert "不插值" in ch["note"]
    assert any("UNKNOWN" in w or "PAUSED" in w for w in sb(c)["data"]["warnings"])                       # 暂停评分的提示


def test_paired_differences_only_on_days_both_lines_ok_with_pair_coverage(tmp_path):
    db, c = client_for(tmp_path)
    pairs = {(p["a"], p["b"]): p for p in sb(c)["data"]["formal"]["pairs"]}
    assert set(pairs) == {("ai", "human_plan"), ("ai", "buyhold")}
    p = pairs[("ai", "human_plan")]
    assert (p["paired_days"], p["planned_days"]) == (3, 7)                                                # 只在两线均 OK 的 3 天配对
    assert p["coverage"]["text"] == "42.86%"
    assert p["cumulative"]["v"] == "0.005" and p["cumulative"]["text"] == "+0.50%"                        # ΣΔ = R_AI(3) − R_HP(3) = 1.5% − 1.0%
    assert p["excluding_ambiguous"]["paired_days"] == 2                                                   # 剔除歧义日的敏感性
    iv = p["interval"]
    assert iv["lo"]["na"] is True and iv["lo"]["text"] == "不可用" and "不触发晋级" in iv["note"]          # 样本不足：不给区间；区间仅描述
    bh = pairs[("ai", "buyhold")]
    assert bh["cumulative"]["v"] == "0.012" and bh["paired_days"] == 3


def test_confidence_interval_is_descriptive_and_deterministic_when_sample_is_enough(tmp_path):
    from mystock2.scoreboard.types import ExecProtocol
    db = build_ops_db(tmp_path, run=False)
    import json as _json
    w = dbmod.connect_writer(db, "scoreboard")
    from datetime import date, timedelta
    w.execute("INSERT INTO eval_run(run_id, batch_id, protocol_version, protocol_json, evidence_json, created_at) VALUES ('RL','B1','exec-v1',?,'[]','2026-03-11T05:00:00Z')",
              (_json.dumps(ExecProtocol().as_dict()),))
    d0 = date(2026, 3, 2)
    for kind, step in (("ai", 3), ("human_plan", 1)):
        for i in range(24):
            w.execute("INSERT INTO sleeve_daily(run_id, line_id, currency, date, status, equity, flags_json) VALUES ('RL',?,'USD',?,'OK',?,'[]')",
                      (f"B1:{kind}", (d0 + timedelta(days=i)).isoformat(), str(10000 + step * (i + 1) + (i % 3) * 2)))
    w.close()
    c = make_app(tmp_path, db).test_client()
    p = next(x for x in sb(c)["data"]["formal"]["pairs"] if x["b"] == "human_plan")
    assert p["paired_days"] == 24 and p["coverage"]["text"] == "100.00%"
    assert not p["interval"]["lo"].get("na") and "不触发晋级" in p["interval"]["note"]
    again = next(x for x in sb(make_app(tmp_path, db).test_client())["data"]["formal"]["pairs"] if x["b"] == "human_plan")
    assert again["interval"] == p["interval"]                                                             # 固定种子：同输入同输出


def test_scoreboard_without_a_run_shows_unavailable_not_zero(tmp_path):
    db, c = client_for(tmp_path, run=False)
    b = sb(c)
    d = b["data"]
    assert d["run"] is None and d["formal"]["chart"] is None
    for ln in d["formal"]["lines"]:
        for k in ("cumulative_return", "max_drawdown", "turnover", "coverage"):
            assert ln["metrics"][k]["na"] is True and ln["metrics"][k]["text"] == "不可用" and ln["metrics"][k]["v"] is None
    assert all(p["available"] is False for p in d["formal"]["pairs"])
    assert any("还没有记分牌 run" in w for w in d["warnings"])
    assert b["header"]["staleness"]["label"] == "未知"                                                   # 没有 run → 新鲜度「未知」


def test_scoreboard_protocol_state_hash_and_pilot_flags(tmp_path):
    db, c = client_for(tmp_path)
    pr = sb(c)["data"]["protocol"]
    ro = dbmod.connect_ro(db)
    assert pr["batch_state_hash"] == ro.execute("SELECT state_hash FROM comparison_batch").fetchone()[0]
    assert pr["exec_protocol"]["version"] == "exec-v1" and pr["exec_protocol"]["params"]["max_participation"] == "0.1"
    assert pr["pilot"] == {"value": False, "reasons": []} and pr["coach_protocols"][0]["frozen"] is True and pr["coach_protocols"][0]["version"] == "test-1"
    # 没有冻结登记 → pilot
    (tmp_path / "a").mkdir()
    db2 = build_ops_db(tmp_path / "a", protocol=False)
    pr2 = sb(make_app(tmp_path / "a", db2).test_client())["data"]["protocol"]
    assert pr2["pilot"]["value"] is True and any("没有冻结登记" in r for r in pr2["pilot"]["reasons"])
    # 以 pilot 冻结 → pilot，并列出缺失项
    (tmp_path / "b").mkdir()
    db3 = build_ops_db(tmp_path / "b", protocol=False)
    seed_protocol(db3, pilot=True)
    pr3 = sb(make_app(tmp_path / "b", db3).test_client())["data"]["protocol"]
    assert pr3["pilot"]["value"] is True and any("strategy.max_hold_days" in r for r in pr3["pilot"]["reasons"])
    # 冻结晚于首张单 → 这些单不是确认样本
    (tmp_path / "c").mkdir()
    db4 = build_ops_db(tmp_path / "c", protocol=False)
    from datetime import datetime, timezone
    seed_protocol(db4, frozen_at=datetime(2026, 3, 11, 0, 0, tzinfo=timezone.utc))
    pr4 = sb(make_app(tmp_path / "c", db4).test_client())["data"]["protocol"]
    assert pr4["pilot"]["value"] is True and any("冻结之前就已生成操作单" in r for r in pr4["pilot"]["reasons"])


def test_scoreboard_run_selection_old_runs_are_kept_and_mismatch_is_rejected(tmp_path):
    db, c = client_for(tmp_path)
    seed_run(db, run_id="R2", created="2026-03-11T05:30:00Z", ai_eq=["10100", "10200", "10150", "10100", "10100", "10100", "10100"], ai_status=["OK"] * 7)
    c = make_app(tmp_path, db).test_client()
    latest = sb(c)["data"]
    assert latest["run"]["run_id"] == "R2" and [r["run_id"] for r in latest["runs"]] == ["R2", "R1"]
    assert line(sb(c), "ai")["metrics"]["days_ok"] == 7                                                   # 默认最新 run
    old = sb(c, run="R1")["data"]
    assert old["run"]["run_id"] == "R1" and line(sb(c, run="R1"), "ai")["metrics"]["days_ok"] == 3        # 旧 run 保留可查
    assert get_view(c, "scoreboard", run="nope")[1]["error"]["code"] == "run_not_found"
    assert get_view(c, "scoreboard", run="R1", batch="OTHER")[1]["error"]["code"] in ("run_batch_mismatch", "batch_not_found")
    assert get_view(c, "scoreboard", batch="zzz")[1]["error"]["code"] == "batch_not_found"


def test_scoreboard_unavailable_without_a_batch_and_stored_metrics_mismatch_is_flagged(tmp_path):
    p = tmp_path / "e.db"
    dbmod.migrate(p)
    assert get_view(make_app(tmp_path, p).test_client(), "scoreboard")[1]["error"]["code"] == "no_batch"
    (tmp_path / "m").mkdir()
    db = build_ops_db(tmp_path / "m", run=False)
    seed_run(db, metrics_json=json.dumps({"B1:ai": {"cumulative_return": "0.99", "days_ok": 3, "fills": 1}}))
    b = sb(make_app(tmp_path / "m", db).test_client())
    assert any("不一致" in w and "B1:ai" in w for w in b["data"]["warnings"])


def test_scoreboard_without_human_actual_line_marks_it_unavailable(tmp_path):
    (tmp_path / "n").mkdir()
    db = build_demo_db(tmp_path / "n")
    seed_batch(db, kinds=("ai", "human_plan", "buyhold"))
    seed_run(db, with_actual=False)
    d = sb(make_app(tmp_path / "n", db).test_client())["data"]
    assert d["descriptive"]["available"] is False and d["descriptive"]["lines"] == [] and d["descriptive"]["chart"] is None


# ====================================================================== 复盘
def rp(client, **q):
    code, body = get_view(client, "replay", **q)
    assert code == 200 and body["status"] == "ok", body
    return body["data"]


def test_replay_cards_split_facts_evidence_diagnosis_outcome_gaps_and_motive_not_recorded(tmp_path):
    db, c = client_for(tmp_path)
    d = rp(c)
    rows = d["cards"]["rows"]
    assert d["cards"]["total"] == 4 and [r["deal_id"] for r in rows] == ["d4", "d2", "d1", "d3"]
    for r in rows:
        assert r["motive"]["text"] == "动机未记录" and r["motive"]["tag"] == "缺口"
        det = r["detail"]
        assert set(det) >= {"facts", "evidence", "diagnosis", "gaps", "outcome"}
        assert "动机未记录" in " ".join(det["gaps"]) and "无事前意图（动机未记录）" in det["evidence"]
        assert [o["days"] for o in r["outcomes"]] == [1, 5, 20]
    d2 = next(r for r in rows if r["deal_id"] == "d2")
    assert d2["side"] == "SELL" and d2["price"]["text"] == "110.00 USD" and d2["fee"]["text"] == "1.00 USD"
    assert d2["range_position"]["v"] is not None
    assert all(o["change"]["na"] for o in d2["outcomes"][1:])                                            # 未到期：不可用，不记零
    d4 = next(r for r in rows if r["deal_id"] == "d4")
    assert d4["fee"]["na"] is True and "费用未记录" in " ".join(d4["detail"]["gaps"])
    assert "不是重撮合" in d["cards"]["note"] and "未实现" in d["cards"]["note"]    # 对照未实现，如实标注


def test_replay_behavior_metrics_show_insufficient_with_sample_size(tmp_path):
    db, c = client_for(tmp_path)
    beh = rp(c)["behavior"]
    assert beh["min_sample"] == 5 and len(beh["rows"]) >= 7
    buy = next(r for r in beh["rows"] if r["name"].startswith("买入执行质量"))
    assert buy["insufficient"] is True and buy["value"]["text"] == "不足" and buy["value"]["v"] is None and buy["n"] == 2 and buy["sample"] == "n=2"
    assert all(r["value"]["text"] != "0" and r["value"]["text"] != "0.00" for r in beh["rows"] if r["insufficient"])      # 不显示 0
    assert "不足" in beh["note"]


def test_replay_behavior_metrics_show_values_when_sample_is_enough(tmp_path):
    db = build_ops_db(tmp_path, status=False, tickets=False, run=False, protocol=False)
    from .test_web_fixtures import buy
    led = dbmod.connect_writer(db, "ledger")
    for i, day in enumerate(["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-10"]):
        buy(led, f"x{i}", "US.NVDA", 1, 100 + i, f"{day}T16:00:00Z")
    led.close()
    d = rp(make_app(tmp_path, db).test_client(), code="US.NVDA")
    buyq = next(r for r in d["behavior"]["rows"] if r["name"].startswith("买入执行质量"))
    assert buyq["n"] >= 5 and buyq["insufficient"] is False and buyq["value"]["text"] not in ("不足", "0")
    assert d["code_filter"] == "US.NVDA" and all(r["code"] == "US.NVDA" for r in d["cards"]["rows"])


def test_replay_rounds_are_labelled_diagnostic_and_not_the_ledger_pnl_basis(tmp_path):
    db, c = client_for(tmp_path)
    rd = rp(c)["rounds"]
    assert rd["tag"] == "诊断回合" and "不是账本收益口径" in rd["note"] and all(r["tag"] == "诊断回合" for r in rd["rows"])
    nv = next(r for r in rd["rows"] if r["code"] == "US.NVDA")
    assert nv["pnl"]["na"] is True and any("成本未知" in f for f in nv["flags"])                          # 期初库存无成本证据：不给盈亏
    assert nv["cost_unit"]["na"] is True and nv["currency"] == "USD"
    assert any(u["code"] == "HK.00700" for u in rd["unclosed"])                                          # 未平仓部分单列，不算胜负
    # 与盈亏视图分区：回合行里没有「已实现盈亏」口径字段
    assert "realized" not in json.dumps(rd)


def test_replay_ai_ticket_evidence_is_sealed_until_revealed(tmp_path):
    db, c = client_for(tmp_path)
    text = raw(c, "replay")
    for v in (OLD_LIMIT, OLD_QTY, SENT_LIMIT, SENT_QTY, "edge_ok"):
        assert v not in text
    d2 = next(r for r in json.loads(text)["data"]["cards"]["rows"] if r["deal_id"] == "d2")
    assert d2["detail"]["ai_tickets"][0]["state"] == "sealed" and "已密封" in " ".join(d2["detail"]["evidence"])
    assert "action" not in d2["detail"]["ai_tickets"][0]
    add_reveal(db, target=OLD_TARGET)                                                                     # 揭示后卡片才显示 AI 单内容
    d2 = next(r for r in rp(c)["cards"]["rows"] if r["deal_id"] == "d2")
    t = d2["detail"]["ai_tickets"][0]
    assert t["state"] == "revealed" and t["limit_price"]["v"] == OLD_LIMIT and t["qty"]["v"] == OLD_QTY and "买入" in " ".join(d2["detail"]["evidence"])


def test_replay_limit_and_account_params(tmp_path):
    db, c = client_for(tmp_path)
    d = rp(c, limit=2)
    assert d["cards"]["shown"] == 2 and d["cards"]["total"] == 4
    assert get_view(c, "replay", limit=0)[0] == 400 and get_view(c, "replay", account="zz")[1]["error"]["code"] == "account_not_found"


# ====================================================================== 数据状态
def ds(client, **q):
    code, body = get_view(client, "data_status", **q)
    assert code == 200 and body["status"] == "ok", body
    return body["data"]


def test_data_status_collection_flags_failed_empty_stale_and_last_success(tmp_path):
    db, c = client_for(tmp_path)
    d = ds(c)
    rows = {(r["code"], r["kind"]): r for r in d["collection"]}
    nv = rows[("US.NVDA", "daily")]
    assert nv["state"] == "error" and nv["state_text"] == "最近一次失败" and ensure_utc(nv["last_ok_at"]) == ensure_utc("2026-03-10T23:00:00Z") and nv["last_detail"] == "HTTP 429 限流"
    assert nv["counts"]["ok"] == 1 and nv["counts"]["error"] == 1
    ts = rows[("US.TSLA", "daily")]
    assert ts["state"] == "never_ok" and ts["last_ok_at"] is None and ts["last_ok_age"]["na"] is True and ts["last_status"] == "empty"
    assert rows[("US.OLD", "daily")]["state"] == "stale_age" and "陈旧" in rows[("US.OLD", "daily")]["state_text"]
    assert rows[("HK.00700", "daily")]["state"] == "ok" and rows[("USDHKD", "fx")]["state"] == "ok"
    assert [r["problem"] for r in d["collection"]] == sorted((r["problem"] for r in d["collection"]), reverse=True)        # 有问题的排在前面
    assert d["summary"]["collection_problems"] == 3
    assert {r["code"]: r["state"] for r in ds(c, collect_stale_hours=2000)["collection"]}["US.OLD"] == "ok"      # 阈值可调


def test_data_status_quote_gaps_use_the_trading_calendar(tmp_path):
    db, c = client_for(tmp_path)
    q = {r["code"]: r for r in ds(c)["quotes"]}
    nv = q["US.NVDA"]
    assert nv["expected_session"] == "2026-03-10" and nv["latest_daily"]["session_date"] == "2026-03-10" and nv["lag_sessions"] == 0
    assert nv["missing_sessions"] == ["2026-03-09"] and nv["missing_count"] == 1 and nv["state"] == "gaps"          # 03-09 缺行情
    assert nv["latest_hourly"]["complete"] is True and ensure_utc(nv["latest_hourly"]["bar_start"]) == ensure_utc("2026-03-10T19:30:00Z")
    hk = q["HK.00700"]
    assert hk["missing_count"] == 0 and hk["state"] == "ok" and hk["latest_hourly"] is None
    assert q["US.TSLA"]["state"] == "no_quotes" and q["US.TSLA"]["latest_daily"] is None and q["US.TSLA"]["missing_count"] is None   # 只有采集回执、没有行情：不可用
    assert {r["pair"] for r in ds(c)["fx"]} == {"USDHKD", "USDCNY"}


def test_data_status_quotes_behind_calendar_are_flagged(tmp_path):
    db = build_ops_db(tmp_path, status=False)
    mk = dbmod.connect_writer(db, "market")
    from datetime import date

    from mystock2.market.bars import DailyBar, put_daily
    put_daily(mk, [DailyBar("US.NEW", date(2026, 3, 5), "10", "11", "9", "10", "10", "1")], source="s", received_at="2026-03-06T00:00:00Z")
    mk.close()
    r = next(x for x in ds(make_app(tmp_path, db).test_client())["quotes"] if x["code"] == "US.NEW")
    assert r["lag_sessions"] == 3 and r["state"] == "behind" and "落后 3 个交易日" in r["state_text"]                    # 03-06、03-09、03-10 缺
    assert r["missing_count"] == 3


def test_data_status_predictions_are_counts_and_dates_only(tmp_path):
    db, c = client_for(tmp_path)
    text = raw(c, "data_status")
    for v in (SENT_PRED_LOW, SENT_PRED_HIGH, SENT_Y_LOW, SENT_Y_HIGH, "y_low", "y_high", "low_price", "high_price"):
        assert v not in text                                                                                    # 预测区间属于密封内容
    p = json.loads(text)["data"]["predictions"]
    assert p["total"] == 3 and p["latest_target_session"] == TARGET and p["by_model"][0]["count"] == 3
    assert {r["code"]: r["count"] for r in p["by_code"]} == {"US.NVDA": 2, "HK.00700": 1}


def test_data_status_runs_receipts_without_free_text_and_dangling_runs_flagged(tmp_path):
    db, c = client_for(tmp_path)
    text = raw(c, "data_status")
    assert "SECRET_FREE_TEXT_999" not in text                                                                    # 失败详情里的自由文本不回显
    runs = {r["run_id"]: r for r in json.loads(text)["data"]["runs"]["recent"]}
    assert runs["r3"]["status"] == "failed" and runs["r3"]["detail"] == {"error": "ConfigError"} and runs["r3"]["problem"]
    assert runs["r2"]["status"] == "partial" and runs["r2"]["retry_scope"] == "US.TSLA daily"
    assert runs["r1"]["detail"] == {"tickets": 3, "pilot": False} and runs["r1"]["problem"] is False
    assert runs["r4"]["status"] == "running" and runs["r4"]["problem"] and "中断" in runs["r4"]["note"]
    assert json.loads(text)["data"]["summary"]["run_problems"] == 3
    assert len(ds(c, runs=2)["runs"]["recent"]) == 2


def test_data_status_protocol_freeze_and_pilot_state(tmp_path):
    db, c = client_for(tmp_path)
    pr = ds(c)["protocols"]
    assert pr["verdict"] == {"frozen": True, "pilot": False, "text": "已冻结且完整（非 pilot）"} and pr["rows"][0]["protocol_version"] == "test-1"
    (tmp_path / "p").mkdir()
    d0 = build_ops_db(tmp_path / "p", protocol=False)
    v = ds(make_app(tmp_path / "p", d0).test_client())["protocols"]["verdict"]
    assert v["frozen"] is False and v["pilot"] is True and "所有记录标 pilot" in v["text"]
    (tmp_path / "q").mkdir()
    d1 = build_ops_db(tmp_path / "q", protocol=False)
    seed_protocol(d1, pilot=True)
    pv = ds(make_app(tmp_path / "q", d1).test_client())["protocols"]
    assert pv["verdict"]["pilot"] is True and pv["rows"][0]["missing"] == ["strategy.max_hold_days"]


def test_data_status_snapshots_and_empty_database_is_unknown_not_fresh(tmp_path):
    db, c = client_for(tmp_path)
    s = ds(c)["snapshots"]
    assert s[0]["snapshots"] >= 1 and s[0]["captured_at"] and s[0]["positions"] == 2
    p = tmp_path / "empty.db"
    dbmod.migrate(p)
    code, body = get_view(make_app(tmp_path, p).test_client(), "data_status")
    assert body["status"] == "ok" and body["header"]["staleness"]["label"] == "未知" and body["data"]["collection"] == [] and body["data"]["quotes"] == []
    assert body["data"]["predictions"]["total"] == 0 and body["data"]["predictions"]["latest_target_session"] is None


def test_new_views_have_freshness_headers_and_use_the_injected_clock(tmp_path):
    db, c = client_for(tmp_path)
    for vid in ("tickets", "scoreboard", "replay", "data_status"):
        code, body = get_view(c, vid)
        h = body["header"]
        assert ensure_utc(h["generated_at"]) == ensure_utc("2026-03-11T06:00:00Z") and h["staleness"]["label"] in ("新鲜", "陈旧", "未知"), vid
        assert h["sources"], vid


def test_group_atomic_selection_a_partial_refresh_does_not_splice_older_group(tmp_path):
    """整组原子选择（§6A.3）：盘前刷新只覆盖部分标的时，其余标的按无订单处理，不拼接收盘组的旧单（与记分牌引擎同一函数）。"""
    from .test_web_opsdata import T_PREOPEN, _draft, _freeze
    db = build_ops_db(tmp_path, tickets=False, reveal_target=True)
    w = dbmod.connect_writer(db, "coach")
    _freeze(w, line="ai", target=TARGET, stage="close", now=T_PREOPEN.replace(hour=1), drafts=[_draft("US.NVDA", "BUY", "120", 10, ("edge_ok",)), _draft("US.TSLA", "BUY", "200", 5, ("edge_ok",))])
    _freeze(w, line="ai", target=TARGET, stage="preopen", now=T_PREOPEN, drafts=[_draft("US.NVDA", "BUY", "121", 11, ("edge_ok",))])
    w.close()
    rows = {r["code"]: r for r in mkt(get_view(make_app(tmp_path, db).test_client(), "tickets")[1])["rows"]}
    assert rows["US.NVDA"]["effective"]["code"] == "selected" and rows["US.NVDA"]["qty"]["v"] == "11"
    assert rows["US.TSLA"]["effective"]["code"] == "not_in_latest_group" and "不拼接旧组" in rows["US.TSLA"]["effective"]["text"]
    assert rows["US.TSLA"]["qty"]["v"] == "5"                                   # 已揭示时仍显示这条记录本身（标明不采用）
