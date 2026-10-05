"""M6 端到端（合成数据）：批次→协议→教练密封→意图/暴露→冻结→记分牌重算（SB-01：CLI 数值＝库内数值）。"""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT
from mystock2.instruments.security_rule import put_rule
from mystock2.market.bars import DailyBar, HourlyBar, put_daily, put_hourly
from tests.unit.market_helpers import synth_bars

UTC = timezone.utc
CODE = "US.NVDA"
T = date(2026, 3, 4)          # 数据截至
TARGET = date(2026, 3, 5)     # 目标日


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "e.db"
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["US"]},
                                   "db": {"path": str(db)}, "web": {"host": "127.0.0.1", "port": 8889}}), encoding="utf-8")
    local = tmp_path / "local"
    local.mkdir()
    (local / "universe.yaml").write_text(yaml.safe_dump({"instruments": [{"code": CODE, "tier": "trade", "max_weight": "0.5", "max_lots": 1000}]}), encoding="utf-8")
    (local / "fees.yaml").write_text("rules:\n  - {profile_id: syn, market: US, side: ANY, basis: order, currency: USD, pct_fee: '0.001', min_fee: '1'}\n", encoding="utf-8")
    protocol = {"protocol_version": "test-1", "strategy_version": "inv-policy-v1",
                "strategy": {"k": "0.5", "q_buy": "0.2", "q_sell": "0.8", "min_gain": "0.01", "max_hold_days": 5, "exit_q": "0.3", "budget_slice": "0.3",
                             "allow_add": False, "exit_order": {"type": "limit", "unfilled": "carry"}},
                "human_plan": {"constraint_handling": "truncate"}, "settlement": {"US": 1}, "execution": {"max_participation": "0.5"}}
    (local / "protocol.yaml").write_text(yaml.safe_dump(protocol), encoding="utf-8")
    dbmod.migrate(db)
    m, f, i = (dbmod.connect_writer(db, o) for o in ("market", "forecast", "instruments"))
    put_rule(i, CODE, "2026-01-01", lot_size=1, tick_json='[{"tick":"0.01"}]', source="合成", verified=True)
    bars = synth_bars(CODE, 330, T, seed=5)
    recv = datetime(2026, 3, 4, 22, 0, tzinfo=UTC)
    put_daily(m, bars, source="syn", received_at=recv, quality="ok")
    # 目标日：行情（低价足够低以穿越买入限价，量充足）与收盘价
    s = cal.session("US", TARGET)
    px = float(bars[-1].close)
    put_hourly(m, [HourlyBar(CODE, s.open_utc + timedelta(hours=k), min(s.open_utc + timedelta(hours=k + 1), s.close_utc), f"{px:.2f}", f"{px * 1.2:.2f}",
                              f"{px * 0.5:.2f}", f"{px:.2f}", "50000000", True) for k in range(7)], source="syn", received_at=datetime(2026, 3, 5, 22, tzinfo=UTC))
    put_daily(m, [DailyBar(CODE, TARGET, f"{px:.2f}", f"{px * 1.2:.2f}", f"{px * 0.5:.2f}", f"{px * 1.01:.2f}", f"{px * 1.01:.2f}", "1")], source="syn",
              received_at=datetime(2026, 3, 5, 22, tzinfo=UTC), quality="ok")
    return {"cfg": str(cfg), "local": str(local), "db": db, "px": px, "protocol": protocol}


def cli(env, *args, clock=True):
    import os
    e = {**os.environ, **({"MYSTOCK2_ALLOW_NOW_OVERRIDE": "1"} if clock else {})}
    r = subprocess.run([sys.executable, "-m", "mystock2", "--config", env["cfg"], *args], capture_output=True, text=True, cwd=REPO_ROOT, env=e)
    return r


NOW_CLOSE = "2026-03-04T22:30:00+00:00"


def test_full_daily_flow_sealed_output_exposure_and_consistent_scoreboard(env):
    loc = ["--local-dir", env["local"]]
    r = cli(env, "protocol", "freeze", *loc)
    assert r.returncode == 0 and '"pilot": false' in r.stdout
    r = cli(env, "batch", "create", "--id", "B1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000", *loc)
    assert r.returncode == 0 and "e0=10000" in r.stdout, r.stderr

    # 教练运行：密封——输出里没有动作/限价/数量
    r = cli(env, "coach", "run", "--batch", "B1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc)
    assert r.returncode == 0, r.stderr
    assert "tickets_frozen=1" in r.stdout and "pilot=False" in r.stdout
    for leak in ("BUY", "SELL", "limit", "qty=", "limit_price"):
        assert leak not in r.stdout and leak not in r.stderr
    run_id = [x for x in r.stdout.split() if x.startswith("run_id=")][0]
    assert run_id.startswith("run_id=r_")
    status = cli(env, "coach", "status", "--batch", "B1", "--market", "US")
    assert "close" in status.stdout and "frozen" in status.stdout and "exposures=0" in status.stdout
    assert "BUY" not in status.stdout

    # 揭示之前：人类线自己的状态 + 记录计划（seen_ai=0）
    st = cli(env, "intent", "state", "--batch", "B1", "--target", TARGET.isoformat(), *loc)
    assert st.returncode == 0 and json.loads(st.stdout)["cash"] == "10000"
    px = Decimal(str(env["px"]))
    r = cli(env, "intent", "add", "--batch", "B1", "--target", TARGET.isoformat(), "--code", CODE, "--action", "buy", "--limit", f"{px:.2f}", "--qty", "20",
            "--now", "2026-03-05T12:00:00+00:00", *loc)
    assert r.returncode == 0 and "seen_ai=0" in r.stdout and "late_record=0" in r.stdout, r.stdout + r.stderr

    # 受控揭示：打印 AI 单并写暴露日志
    r = cli(env, "coach", "show", "--batch", "B1", "--market", "US", "--target", TARGET.isoformat(), "--now", "2026-03-05T12:10:00+00:00")
    assert r.returncode == 0 and "BUY" in r.stdout and "已写暴露日志" in r.stdout
    assert "exposures=1" in cli(env, "coach", "status", "--batch", "B1").stdout
    # 揭示之后修改：seen_ai=1、late_record=1，不进入 human_plan 线
    r = cli(env, "intent", "add", "--batch", "B1", "--target", TARGET.isoformat(), "--code", CODE, "--action", "buy", "--limit", f"{px:.2f}", "--qty", "5",
            "--now", "2026-03-05T12:30:00+00:00", *loc)
    assert "seen_ai=1" in r.stdout and "late_record=1" in r.stdout
    # 截止时冻结人类计划：取揭示前的 20 股
    r = cli(env, "intent", "freeze", "--batch", "B1", "--target", TARGET.isoformat(), "--now", "2026-03-05T12:50:00+00:00", *loc)
    assert r.returncode == 0 and "human_plan_tickets=1" in r.stdout and "plan_missing=0" in r.stdout

    # 记分牌：CLI 打印的数值＝库内 eval_run.metrics_json（SB-01）
    r = cli(env, "scoreboard", "run", "--batch", "B1", "--end", TARGET.isoformat(), *loc)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    ro = dbmod.connect_ro(env["db"])
    row = ro.execute("SELECT metrics_json FROM eval_run WHERE run_id=?", (out["run_id"],)).fetchone()
    assert json.loads(row["metrics_json"]) == out["lines"]                          # 同一 run：CLI 与库一致
    ai, human = out["lines"]["B1:ai"], out["lines"]["B1:human_plan"]
    assert ai["days_ok"] == 1 and human["days_ok"] == 1 and ai["fills"] >= 1 and human["fills"] == 1
    fills = ro.execute("SELECT line_id, qty FROM sim_fill ORDER BY line_id").fetchall()
    assert [f["qty"] for f in fills if f["line_id"] == "B1:human_plan"] == ["20"]    # 揭示前锁定的 20 股，不是修改后的 5
    assert "ai_vs_human_plan" in out and out["ai_vs_human_plan"]["n"] == 1
    # 再跑一次：新 run，旧 run 保留（不覆盖）
    r2 = cli(env, "scoreboard", "run", "--batch", "B1", "--end", TARGET.isoformat(), *loc)
    assert json.loads(r2.stdout)["run_id"] != out["run_id"]
    assert ro.execute("SELECT COUNT(*) c FROM eval_run").fetchone()["c"] == 2


def test_incomplete_protocol_refuses_freeze_and_runs_are_pilot(env, tmp_path):
    proto = dict(env["protocol"])
    proto["strategy"] = {k: v for k, v in proto["strategy"].items() if k != "max_hold_days"}
    (Path(env["local"]) / "protocol.yaml").write_text(yaml.safe_dump(proto), encoding="utf-8")
    loc = ["--local-dir", env["local"]]
    r = cli(env, "protocol", "freeze", *loc)
    assert r.returncode == 2 and "strategy.max_hold_days" in r.stderr
    assert cli(env, "protocol", "freeze", "--pilot", *loc).returncode == 0
    assert cli(env, "batch", "create", "--id", "B1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000", *loc).returncode == 0
    r = cli(env, "coach", "run", "--batch", "B1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc)
    assert "pilot=True" in r.stdout
    row = dbmod.connect_ro(env["db"]).execute("SELECT action, reason_json FROM ticket").fetchone()
    assert row["action"] == "SKIP" and "params_missing:max_hold_days" in row["reason_json"]       # 缺参数不出可执行数量


def test_template_protocol_cannot_be_frozen_and_missing_local_dir_errors(env, tmp_path):
    shutil_proto = yaml.safe_load((REPO_ROOT / "config" / "protocol.example.yaml").read_text(encoding="utf-8"))
    (Path(env["local"]) / "protocol.yaml").write_text(yaml.safe_dump(shutil_proto), encoding="utf-8")
    r = cli(env, "protocol", "freeze", "--pilot", "--local-dir", env["local"])
    assert r.returncode == 2 and "TEMPLATE" in r.stderr
    r = cli(env, "protocol", "freeze", "--local-dir", str(tmp_path / "nope"))
    assert r.returncode == 1 and "缺少" in r.stderr


def test_replay_cli_prints_cards_and_metrics_with_sample_sizes(env):
    from mystock2.ledger import opening
    from mystock2.ledger.events import EventDraft, ensure_account, fill_key, post_event
    led = dbmod.connect_writer(env["db"], "ledger")
    ensure_account(led, "A1", "futu", "REAL", "USD")
    opening.record_opening(led, "A1", "2026-03-01T00:00:00.000000Z", {}, {"USD": "100000"})
    px = Decimal(str(env["px"])).quantize(Decimal("0.01"))
    post_event(led, EventDraft(fill_key("A1", "D1"), "A1", "FILL", "2026-03-05T15:00:00.000000Z", "USD", code=CODE, price=str(px), qty_delta="3", cash_delta=str(-px * 3), ref_deal_id="D1"))
    r = cli(env, "replay", "cards", "--account", "A1")
    assert r.returncode == 0 and "【事实】" in r.stdout and "动机未记录" in r.stdout and "事后诊断" in r.stdout
    r = cli(env, "replay", "behavior", "--account", "A1")
    assert r.returncode == 0 and "不足" in r.stdout and "n=" in r.stdout


def test_veto_cli_flow_sealed_export_exposure_and_ai_vs_ai_veto_lines(env):
    loc = ["--local-dir", env["local"]]
    assert cli(env, "protocol", "freeze", *loc).returncode == 0
    assert cli(env, "batch", "create", "--id", "B2", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000",
               "--lines", "ai,ai_veto,human_plan,buyhold", *loc).returncode == 0
    r = cli(env, "coach", "run", "--batch", "B2", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc)
    assert "tickets_frozen=2" in r.stdout                                              # ai 与 ai_veto 各从自己的状态生成基础单
    # 导出：未确认字段 → 拒绝并列出白名单；未记录人类计划 → 拒绝
    r = cli(env, "veto", "export", "--batch", "B2", "--target", TARGET.isoformat(), *loc)
    assert r.returncode == 2 and "白名单" in r.stderr and "tickets[].limit_price" in r.stderr
    r = cli(env, "veto", "export", "--batch", "B2", "--target", TARGET.isoformat(), "--confirm-fields", "--now", "2026-03-05T12:00:00+00:00", *loc)
    assert r.returncode == 2 and "先为" in r.stderr
    # 先记录人类计划（no_trade），再导出
    assert cli(env, "intent", "add", "--batch", "B2", "--target", TARGET.isoformat(), "--code", CODE, "--action", "no_trade", "--now", "2026-03-05T11:00:00+00:00", *loc).returncode == 0
    out = Path(env["db"]).parent / "pack.md"
    r = cli(env, "veto", "export", "--batch", "B2", "--target", TARGET.isoformat(), "--confirm-fields", "--now", "2026-03-05T12:00:00+00:00", "--out", str(out), *loc)
    assert r.returncode == 0 and "channel=veto_export" in r.stdout, r.stderr
    pack_id = [x for x in r.stdout.split() if x.startswith("pack_id=")][0].split("=")[1]
    assert pack_id in out.read_text(encoding="utf-8")
    assert "exposures=1" in cli(env, "coach", "status", "--batch", "B2").stdout          # 导出本身是一次受控揭示
    # 导入人工否决（取消买单）
    resp = {"pack_id": pack_id, "verdict": "downgrade", "adjustments": [{"code": CODE, "type": "cancel_buy"}], "flags": ["earnings_within_2d"],
            "evidence_ids": [], "note": "x"}
    f = Path(env["db"]).parent / "resp.json"
    f.write_text(json.dumps(resp), encoding="utf-8")
    r = cli(env, "veto", "import", "--pack", pack_id, "--response", str(f), "--now", "2026-03-05T12:20:00+00:00", *loc)
    assert r.returncode == 1 and "evidence_required" in r.stdout                       # 有标签/调整必须引用证据：被拒，机械单照常
    resp["evidence_ids"] = []
    resp["flags"] = []
    f.write_text(json.dumps(resp), encoding="utf-8")
    r = cli(env, "veto", "import", "--pack", pack_id, "--response", str(f), "--model-id", "m-1", "--now", "2026-03-05T12:30:00+00:00", *loc)
    assert r.returncode == 1 and "evidence_required" in r.stdout                       # 有 adjustment 也必须引用证据
    out_score = json.loads(cli(env, "scoreboard", "run", "--batch", "B2", "--end", TARGET.isoformat(), *loc).stdout)
    ai, veto = out_score["lines"]["B2:ai"], out_score["lines"]["B2:ai_veto"]
    assert ai["fills"] == veto["fills"] >= 1                                           # 否决被拒 → ai_veto 线与 ai 线一致
    assert dbmod.connect_ro(env["db"]).execute("SELECT COUNT(*) c FROM llm_call WHERE status='rejected'").fetchone()["c"] == 2


def test_collect_quotes_cli_uses_source_fallback_and_writes_receipts(env, monkeypatch, capsys):
    from mystock2.cli import main
    from mystock2.collectors import quotes as q
    from mystock2.market.bars import DailyBar as DB

    class Fake:
        name = "fake"

        def daily(self, code, start, end):
            return [DB(code, date(2026, 3, 3), "10", "11", "9", "10.5", "10.5", "100")]

        def hourly(self, code, start, end):
            return []

        def fx_daily(self, pair, start, end):
            return [(date(2026, 3, 3), "7.8")]
    monkeypatch.setattr(q, "YFinanceSource", Fake)
    rc = main(["--config", env["cfg"], "collect", "quotes", "--codes", "US.AAPL", "--start", "2026-03-02", "--end", "2026-03-04", "--fx", "USDHKD", "--hourly"])
    out = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert rc == 1 and out["US.AAPL"]["status"] in ("ok", "partial") and out["USDHKD"]["status"] == "ok" and out["US.AAPL:hourly"]["status"] == "failed"   # 小时线空：如实失败，不记零
    ro = dbmod.connect_ro(env["db"])
    assert ro.execute("SELECT COUNT(*) c FROM collection_log WHERE kind='hourly' AND status='empty'").fetchone()["c"] == 1
    assert ro.execute("SELECT status FROM run_log WHERE command='collect quotes'").fetchone()["status"] == "partial"


def test_f02_now_override_is_refused_without_the_test_clock_env(env):
    loc = ["--local-dir", env["local"]]
    assert cli(env, "protocol", "freeze", *loc).returncode == 0
    assert cli(env, "batch", "create", "--id", "B1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000", *loc).returncode == 0
    r = cli(env, "coach", "run", "--batch", "B1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc, clock=False)
    assert r.returncode == 1 and "MYSTOCK2_ALLOW_NOW_OVERRIDE" in r.stderr                                 # 生产运行不能回填时间
    r = cli(env, "intent", "add", "--batch", "B1", "--target", TARGET.isoformat(), "--code", CODE, "--action", "no_trade", "--now", "2026-03-05T11:00:00+00:00", *loc, clock=False)
    assert r.returncode == 1 and "MYSTOCK2_ALLOW_NOW_OVERRIDE" in r.stderr


def test_f01_protocol_drift_after_batch_creation_is_refused_and_unfrozen_batches_are_pilot(env):
    loc = ["--local-dir", env["local"]]
    # 未冻结就建批次：标 pilot
    r = cli(env, "batch", "create", "--id", "P1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000", *loc)
    assert r.returncode == 0 and "pilot=True" in r.stdout
    assert "pilot=True" in cli(env, "coach", "run", "--batch", "P1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc).stdout
    # 冻结后建批次：非 pilot；之后改费用档案 → 运行被拒
    assert cli(env, "protocol", "freeze", *loc).returncode == 0
    assert cli(env, "batch", "create", "--id", "F1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "10000", *loc).stdout.count("pilot=False") == 1
    fees = Path(env["local"]) / "fees.yaml"
    fees.write_text(fees.read_text(encoding="utf-8").replace("'0.001'", "'0.002'"), encoding="utf-8")
    r = cli(env, "coach", "run", "--batch", "F1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, *loc)
    assert r.returncode == 1 and "新比较批次" in r.stderr
    r = cli(env, "scoreboard", "run", "--batch", "F1", "--end", TARGET.isoformat(), *loc)
    assert r.returncode == 1 and "不一致" in r.stderr
    r = cli(env, "coach", "run", "--batch", "F1", "--market", "US", "--stage", "close", "--as-of", T.isoformat(), "--now", NOW_CLOSE, "--allow-drift", *loc)
    assert r.returncode == 0 and "pilot=True" in r.stdout                                                   # 调试允许，但结果标 pilot


def test_f09_batch_create_validates_positions_and_includes_other_equity_in_e0(env):
    loc = ["--local-dir", env["local"]]
    cli(env, "protocol", "freeze", *loc)
    base = ["batch", "create", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "1000", *loc]
    r = cli(env, *base, "--id", "X1", "--positions", json.dumps({"US.TSLA": 5}))
    assert r.returncode == 1 and "不是名单内" in r.stderr                                                   # 名单外库存不进正式模拟线
    r = cli(env, "batch", "create", "--id", "X2", "--market", "US", "--d0", T.isoformat(), "--currency", "HKD", "--budget", "1000", *loc, "--positions", json.dumps({CODE: 1}))
    assert r.returncode == 1 and "币种" in r.stderr
    px = Decimal(str(env["px"]))
    close_d0 = dbmod.connect_ro(env["db"]).execute("SELECT close FROM quote_daily WHERE code=? AND session_date=? AND quality='ok' ORDER BY version DESC LIMIT 1", (CODE, T.isoformat())).fetchone()["close"]
    r = cli(env, *base, "--id", "X3", "--positions", json.dumps({CODE: {"qty": 10, "cost": "90", "acquired": "2026-02-01"}}), "--other-equity", "100")
    assert r.returncode == 0, r.stderr
    e0 = Decimal(r.stdout.split("e0=")[1].split()[0])
    assert e0 == Decimal(1000) + 10 * Decimal(close_d0) + 100                                                # E0 = B + 库存市值 + 期初应收（开账 R=0）
    assert "acquired_unknown" not in r.stdout and "cost_estimated" not in r.stdout                           # 给了成本与买入日：不再估计
    r = cli(env, *base, "--id", "X4", "--positions", json.dumps({CODE: 3}))
    assert "cost_estimated:US.NVDA" in r.stdout and "acquired_unknown:US.NVDA" in r.stdout
    _ = px


def test_f10_split_day_state_is_identical_for_coach_and_engine(env):
    from mystock2.cli import ops
    from mystock2.ledger.opening import add_split
    loc_args = ["--local-dir", env["local"]]
    cli(env, "protocol", "freeze", *loc_args)
    assert cli(env, "batch", "create", "--id", "S1", "--market", "US", "--d0", T.isoformat(), "--currency", "USD", "--budget", "1000", *loc_args,
               "--positions", json.dumps({CODE: {"qty": 10, "cost": "100", "acquired": "2026-02-01"}})).returncode == 0
    w = dbmod.connect_writer(env["db"], "ledger")
    add_split(w, CODE, "2026-03-05T12:00:00Z", 2, 1)                                                          # 目标日开盘前 2:1 拆股
    w.close()
    ro = dbmod.connect_ro(env["db"])
    loc = ops.load_local(Path(env["local"]))
    b = ops.load_batch(ro, "S1")
    coach_state, _ = ops.state_at_open(ro, loc, b, "ai", TARGET)
    run = ops.simulate_line(ro, loc, b, "ai", TARGET)
    assert coach_state.qty(CODE) == Decimal(20) and coach_state.hash() == run.start_states[TARGET].hash()     # coach 冻结时的状态哈希＝引擎执行时的状态哈希
