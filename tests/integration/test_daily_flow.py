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


def cli(env, *args):
    r = subprocess.run([sys.executable, "-m", "mystock2", "--config", env["cfg"], *args], capture_output=True, text=True, cwd=REPO_ROOT)
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
