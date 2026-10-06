"""复盘卡 AI 评价：发给模型的输入（脱敏、时间口径）、Codex 调用封装、缓存/刷新/并发、Web 视图与 POST 拉起（合成数据，不调真实模型）。"""
import json
import os
import sqlite3
import stat

import pytest

from mystock2.core import db as dbmod
from mystock2.replay import review_cache as rc
from mystock2.replay.review_payload import PAYLOAD_KEYS, PROMPT_VERSION, input_hash, payload_for, render_prompt
from mystock2.review import codex_runner
from mystock2.review.service import request_review

from .test_web_fixtures import build_demo_db, get_view, make_app

pytestmark = pytest.mark.filterwarnings("ignore")
ACCT, DEAL = "A1", "d1"          # 合成库：NVDA 的第二笔成交


def ro(db):
    return dbmod.connect_ro(db)


class Runner:
    def __init__(self, text="## 结论\n合成评价\n过程评分：3/5 ｜ 结果评分：3/5 ｜ 置信度：中", ok=True, error=None):
        self.calls, self.text, self.ok, self.error = [], text, ok, error

    def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return {"ok": self.ok, "text": self.text if self.ok else "", "error": self.error, "duration_s": 1.5}


# ---------------- 输入与提示词 ----------------

def test_payload_has_only_whitelisted_sections_and_no_identifiers(tmp_path):
    db = build_demo_db(tmp_path)
    p = payload_for(ro(db), ACCT, DEAL)
    assert tuple(p) == PAYLOAD_KEYS
    text = render_prompt(p)
    for secret in (ACCT, DEAL, "deal_id", "account", "acc_id", "cash", "equity", "order_id"):   # 不发账户号、成交号、现金与总权益
        assert secret not in json.dumps(p, ensure_ascii=False), secret
    assert PROMPT_VERSION == "trade-review-v1" and "只使用「数据」里给出的信息" in text and "不要联网" in text


def test_payload_separates_known_same_day_and_hindsight_and_formats_integers(tmp_path):
    db = build_demo_db(tmp_path)
    p = payload_for(ro(db), ACCT, DEAL)
    assert p["trade"]["date"] == "2026-03-03" and p["trade"]["side"] == "BUY"
    assert p["trade"]["qty"] == str(int(p["trade"]["qty"])) and not p["trade"]["qty"].endswith(".")
    ctx = p["known_at_decision"]["market_context_before_trade"]
    assert ctx["available"] and all(s.split()[0] < p["trade"]["date"] for s in ctx["recent_sessions"])      # 成交前的行情只含成交日之前
    assert p["same_day"]["available"] and "不可知" not in json.dumps(p["same_day"])
    assert "hindsight" in p and "只能用来评价结果" in p["hindsight"]["note"]
    later = p["hindsight"]["later_trades_same_instrument"]
    assert all(t["date"] >= p["trade"]["date"] for t in later)


def test_numbers_keep_trailing_zeros_of_integers():
    from mystock2.replay.review_payload import _num
    assert _num("1000", 0) == "1000" and _num("100", 4) == "100" and _num("12.5000", 4) == "12.5" and _num(None) is None and _num("0.0", 2) == "0"


def test_unknown_or_opening_deal_has_no_payload(tmp_path):
    db = build_demo_db(tmp_path)
    assert payload_for(ro(db), ACCT, "nope") is None
    res = request_review(db, {}, ACCT, "nope", runner=Runner())
    assert res["state"] == "error" and "找不到" in res["error"]


def test_input_hash_changes_when_input_changes(tmp_path):
    db = build_demo_db(tmp_path)
    a = payload_for(ro(db), ACCT, DEAL)
    b = json.loads(json.dumps(a))
    b["hindsight"]["later_trades_same_instrument"] = []
    b["trade"]["price"] = "1"
    assert input_hash(a) == input_hash(json.loads(json.dumps(a))) and input_hash(a) != input_hash(b)


# ---------------- 缓存 / 刷新 / 并发 ----------------

def test_first_request_writes_cache_then_reads_cache_and_refresh_adds_a_row(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    r1 = Runner()
    a = request_review(db, {}, ACCT, DEAL, runner=r1)
    assert a["state"] == "ok" and not a["cached"] and len(r1.calls) == 1
    prompt, kw = r1.calls[0]
    assert kw["model"] == "gpt-6.1-sol" and kw["effort"] == "medium" and ACCT not in prompt and DEAL not in prompt
    b = request_review(db, {}, ACCT, DEAL, runner=r1)
    assert b["state"] == "ok" and b["cached"] and len(r1.calls) == 1                 # 第二次读缓存，不再调模型
    r2 = Runner(text="## 结论\n第二版")
    c = request_review(db, {}, ACCT, DEAL, refresh=True, runner=r2)
    assert c["state"] == "ok" and not c["cached"] and len(r2.calls) == 1
    conn = ro(db)
    rows = conn.execute("SELECT status, response_text, request_text FROM trade_review ORDER BY review_id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "ok"] and rows[1]["response_text"] == "## 结论\n第二版"       # 刷新新增一行，旧评价保留
    assert rc.latest_ok(conn, DEAL)["response_text"] == "## 结论\n第二版" and rc.history_count(conn, DEAL) == 2
    assert ACCT not in rows[0]["request_text"] and DEAL not in rows[0]["request_text"]                       # 留痕的提示词同样不含标识


def test_config_controls_model_effort_and_failures_are_recorded_without_losing_old_review(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    r = Runner(ok=False, error="Codex 退出码 1：not logged in")
    res = request_review(db, {"review": {"model": "m-x", "effort": "high", "timeout_s": 5}}, ACCT, DEAL, runner=r)
    assert res["state"] == "error" and "not logged in" in res["error"]
    assert r.calls[0][1]["model"] == "m-x" and r.calls[0][1]["effort"] == "high" and r.calls[0][1]["timeout"] == 5
    ok = request_review(db, {}, ACCT, DEAL, runner=Runner())                          # 失败不算缓存：下一次会重新请求
    assert ok["state"] == "ok" and not ok["cached"]
    bad = request_review(db, {}, ACCT, DEAL, refresh=True, runner=Runner(ok=False, error="boom"))
    assert bad["state"] == "error"
    conn = ro(db)
    assert rc.latest(conn, DEAL)["status"] == "error" and rc.latest_ok(conn, DEAL)["status"] == "ok"      # 旧的成功评价还在


def test_a_second_request_while_running_does_not_call_the_model_again(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    inner = Runner()

    def slow(prompt, **kw):
        again = request_review(db, {}, ACCT, DEAL, refresh=True, runner=inner)   # 运行期间再来一个请求（本进程 pid 存活）
        assert again["state"] == "running" and not inner.calls
        return {"ok": True, "text": "## 结论\nx", "error": None, "duration_s": 0.1}
    assert request_review(db, {}, ACCT, DEAL, runner=slow)["state"] == "ok"


def test_dead_running_row_is_reported_as_interrupted_not_running(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    w = dbmod.connect_writer(db, "review")
    w.execute("INSERT INTO trade_review (deal_id, code, prompt_version, input_hash, model, effort, status, request_text, pid, requested_at) "
              "VALUES (?,?,?,?,?,?,'running','x',?, ?)", (DEAL, "US.NVDA", "v", "h", "m", "e", 99999999, "2026-03-05T00:00:00.000000Z"))
    w.close()
    c = make_app(tmp_path, db).test_client()
    code, body = get_view(c, "trade_review", deal_id=DEAL)
    assert code == 200 and body["data"]["state"] == "error" and "中断" in body["data"]["error"]
    # 中断的 running 行不挡住新请求
    assert request_review(db, {}, ACCT, DEAL, runner=Runner())["state"] == "ok"


def test_writes_go_through_review_owner_only(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    seed_review(db)
    w = dbmod.connect_writer(db, "review")
    with pytest.raises(sqlite3.DatabaseError):
        w.execute("UPDATE ledger_event SET qty_delta='1'")
    with pytest.raises(sqlite3.DatabaseError):
        w.execute("DELETE FROM trade_review")                                   # 只追加：连自己的表也不能删
    w.close()


# ---------------- Codex 调用封装（假 CLI） ----------------

FAKE = """#!/bin/bash
out=""; model=""; eff=""; sandbox=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output-last-message) out="$2";; -m) model="$2";; --sandbox) sandbox="$2";; -c) case "$2" in model_reasoning_effort*) eff="$2";; esac;;
  esac; shift
done
cat > "$FAKE_DUMP"
echo "$model|$eff|$sandbox" > "$FAKE_DUMP.args"
case "$FAKE_MODE" in
  ok) printf '## 结论\\n假评价\\n' > "$out"; echo '{"type":"turn.completed","usage":{"input_tokens":3}}';;
  fail) echo '{"type":"error","message":"not logged in"}'; exit 1;;
  slow) sleep 30;;
  empty) echo '{"type":"turn.completed"}';;
esac
"""


def fake_codex(tmp_path, monkeypatch, mode):
    p = tmp_path / "codex"
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MYSTOCK2_CODEX_BIN", str(p))
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("FAKE_DUMP", str(tmp_path / "prompt.txt"))
    return tmp_path / "prompt.txt"


def test_run_codex_passes_prompt_on_stdin_with_readonly_sandbox_and_requested_model(tmp_path, monkeypatch):
    dump = fake_codex(tmp_path, monkeypatch, "ok")
    r = codex_runner.run_codex("提示词正文", model="gpt-6.1-sol", effort="medium", timeout=20)
    assert r["ok"] and r["text"] == "## 结论\n假评价" and r["usage"] == {"input_tokens": 3}
    assert dump.read_text(encoding="utf-8") == "提示词正文"
    assert (tmp_path / "prompt.txt.args").read_text().strip() == 'gpt-6.1-sol|model_reasoning_effort="medium"|read-only'


def test_run_codex_failure_timeout_empty_and_missing_cli(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, "fail")
    r = codex_runner.run_codex("x", timeout=20)
    assert not r["ok"] and "not logged in" in r["error"]
    fake_codex(tmp_path, monkeypatch, "slow")
    r = codex_runner.run_codex("x", timeout=1)
    assert not r["ok"] and "超时" in r["error"]
    fake_codex(tmp_path, monkeypatch, "empty")
    assert not codex_runner.run_codex("x", timeout=20)["ok"]
    monkeypatch.setenv("MYSTOCK2_CODEX_BIN", str(tmp_path / "nope"))
    with pytest.raises(codex_runner.CodexError):
        codex_runner.run_codex("x")


# ---------------- Web：视图（只读）与 POST（只拉起 CLI） ----------------

def seed_review(db, status="ok", text="## 结论\n缓存评价", h="old-hash", pid=None, requested="2026-03-05T00:00:00.000000Z"):
    w = dbmod.connect_writer(db, "review")
    w.execute("INSERT INTO trade_review (deal_id, code, prompt_version, input_hash, model, effort, status, request_text, response_text, error, pid, requested_at, finished_at, duration_s) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (DEAL, "US.NVDA", PROMPT_VERSION, h, "gpt-6.1-sol", "medium", status, "req", text if status == "ok" else None, "boom" if status == "error" else None,
               pid, requested, requested, 2.0))
    w.close()


def test_view_states_none_ok_stale_and_error_with_old_review(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    c = make_app(tmp_path, db).test_client()
    assert get_view(c, "trade_review", deal_id=DEAL)[1]["data"]["state"] == "none"
    assert get_view(c, "trade_review")[0] == 400                                 # deal_id 必填
    seed_review(db)                                                              # 哈希与现在不同 → 输入已变化
    d = get_view(c, "trade_review", deal_id=DEAL)[1]["data"]
    assert d["state"] == "ok" and d["review"]["text"].endswith("缓存评价") and d["stale"] is True and d["history_count"] == 1
    fresh = input_hash(payload_for(ro(db), ACCT, DEAL))
    seed_review(db, h=fresh, text="## 结论\n新评价")
    d = get_view(c, "trade_review", deal_id=DEAL)[1]["data"]
    assert d["review"]["text"].endswith("新评价") and d["stale"] is False and d["error"] is None
    seed_review(db, status="error")
    d = get_view(c, "trade_review", deal_id=DEAL)[1]["data"]
    assert d["state"] == "ok" and d["error"] == "boom" and d["review"]["text"].endswith("新评价")     # 最近失败不吞掉旧评价
    assert "account" not in json.dumps(d["review"])


def test_view_without_migration_is_unavailable(tmp_path):
    db = build_demo_db(tmp_path)
    raw = sqlite3.connect(db)
    raw.execute("DROP TRIGGER trg_trade_review_no_delete")
    raw.execute("DROP TABLE trade_review")                                       # 模拟没应用 0013 的旧库
    raw.commit()
    raw.close()
    c = make_app(tmp_path, db).test_client()
    code, body = get_view(c, "trade_review", deal_id=DEAL)
    assert body["status"] == "unavailable" and "db migrate" in body["error"]["message"]


def post(c, body=None, headers=None):
    h = {"X-MyStock2-Action": "review"} if headers is None else headers
    return c.post("/api/review/deal", json=body if body is not None else {"deal_id": DEAL}, headers=h)


def test_post_spawns_cli_only_when_needed_and_never_writes_the_db(tmp_path, monkeypatch):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    app = make_app(tmp_path, db)
    spawned = []
    app.extensions["mystock2_review_spawn"] = spawned.append
    c = app.test_client()
    r = post(c)
    assert r.status_code == 202 and r.get_json()["state"] == "running" and len(spawned) == 1
    argv = spawned[0]
    assert argv[1:3] == ["-m", "mystock2"] and argv[argv.index("review") + 1] == "deal" and argv[argv.index("--deal-id") + 1] == DEAL and "--refresh" not in argv
    seed_review(db, h="x")

    real_writer = dbmod.connect_writer

    def guarded(c, body=None):
        def no_writer(*a, **k):
            raise AssertionError("Web 进程不得打开写连接")
        monkeypatch.setattr(dbmod, "connect_writer", no_writer)               # POST 处理期间 Web 若试图写库，直接失败
        try:
            return post(c, body)
        finally:
            monkeypatch.setattr(dbmod, "connect_writer", real_writer)
    r = guarded(c)
    assert r.status_code == 200 and r.get_json()["state"] == "ok" and len(spawned) == 1                 # 有缓存：不拉起
    assert guarded(c, {"deal_id": DEAL, "refresh": True}).status_code == 202 and "--refresh" in spawned[1]
    seed_review(db, status="running", pid=os.getpid(), requested="2999-01-01T00:00:00.000000Z")
    assert guarded(c, {"deal_id": DEAL, "refresh": True}).get_json()["state"] == "running" and len(spawned) == 2   # 在跑：不重复拉起


def test_post_guards_headers_origin_params_and_unknown_deal(tmp_path):
    db = build_demo_db(tmp_path)
    dbmod.migrate(db)
    app = make_app(tmp_path, db)
    spawned = []
    app.extensions["mystock2_review_spawn"] = spawned.append
    c = app.test_client()
    assert post(c, headers={}).status_code == 403                                                   # 缺自定义头（跨站表单发不出）
    assert c.post("/api/review/deal", json={"deal_id": DEAL}, headers={"X-MyStock2-Action": "review", "Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/review/deal", json={"deal_id": DEAL}, headers={"X-MyStock2-Action": "review", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert post(c, {"deal_id": "a b; rm -rf"}).status_code == 400 and post(c, {}).status_code == 400
    assert post(c, {"deal_id": "no-such-deal"}).status_code == 404
    assert c.post("/api/review/deal", json={"deal_id": DEAL}, headers={"X-MyStock2-Action": "review"}, environ_overrides={"HTTP_HOST": "evil.example"}).status_code == 403
    assert not spawned
