"""例行更新命令：步骤编排、失败不中断、非零退出、锁、通知（子进程与行情/数据库全部替换为假实现）。"""
import argparse
import fcntl
from datetime import datetime, timezone

import pytest

from mystock2.cli import update as up


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"db:\n  path: {tmp_path / 'x.db'}\nweb:\n  host: 127.0.0.1\n  port: 8889\nfutu:\n  host: 127.0.0.1\n  port: 11111\n  trd_env: REAL\n"
                   "collect:\n  markets: [HK, US]\nupdate:\n  account_id: acct\n  acc_id: 7\n  lookback_days: 10\n", encoding="utf-8")
    monkeypatch.setattr(up, "LOG_DIR", tmp_path / "logs")
    calls = []
    monkeypatch.setattr(up, "codes_for", lambda cfg_, market: ["US.NVDA", "US.TSLA"] if market == "US" else ["HK.00700"])
    monkeypatch.setattr(up, "_universe_codes", lambda: {"US.NVDA", "HK.00700"})
    monkeypatch.setattr(up, "freshness", lambda *a, **k: [])
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)                       # 不存在的 futu 流水映射文件 → 跳过流水步骤
    notes = []
    monkeypatch.setattr(up, "notify", lambda t, x: notes.append((t, x)))

    def fake(argv, timeout=3600):
        calls.append(argv)
        return {"rc": 1 if "fail-me" in " ".join(argv) else 0, "secs": 0.0, "tail": ""}
    monkeypatch.setattr(up, "run_step", fake)
    return cfg, calls, notes


def args(cfg, phase, **kw):
    return argparse.Namespace(config=str(cfg), phase=phase, lookback=None, no_futu=False, notify=False, **kw)


def names(calls):
    out = []
    for a in calls:
        i = a.index("mystock2") + 1
        out.append(" ".join(a[i + 2:i + 4]) if a[i] == "--config" else " ".join(a[i:i + 2]))
    return out


def test_us_phase_runs_futu_quotes_hourly_forecast_reconcile_in_order(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "us")) == 0
    got = [" ".join(a[a.index("mystock2") + 3:a.index("mystock2") + 5]) for a in calls]       # 跳过 --config <path>
    assert got == ["collect futu", "collect quotes", "collect quotes", "forecast run", "forecast run", "ledger reconcile"]
    futu = calls[0]
    assert futu[futu.index("--what") + 1] == "deals,orders,fees,snapshot" and "--assume-market-currency" in futu and futu[futu.index("--acc-id") + 1] == "7"
    daily = calls[1]
    assert daily[daily.index("--codes") + 1] == "US.NVDA,US.TSLA" and "--fx" in daily and "--hourly" in calls[2]
    assert [c[c.index("--model") + 1] for c in calls[3:5]] == ["baseline", "lgbm"] and calls[3][calls[3].index("--codes") + 1] == "US.NVDA"   # 预测只对名单内标的


def test_pre_phase_is_light_and_hk_phase_uses_hk_codes(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "pre")) == 0
    assert [c[c.index("--what") + 1] for c in calls if "--what" in c] == ["orders,snapshot"]                # 盘前：不拉成交/费用，不预测、不对账
    assert not any("forecast" in a or "reconcile" in a for c in calls for a in c)
    calls.clear()
    assert up.cmd_update(args(cfg, "hk")) == 0
    assert any("HK.00700" in a for c in calls for a in c) and not any("US.NVDA" in a for c in calls for a in c)


def test_failures_do_not_stop_later_steps_exit_nonzero_and_notify(env, monkeypatch):
    cfg, calls, notes = env
    seen = []

    def fake(argv, timeout=3600):
        seen.append(argv)
        return {"rc": 1 if "futu" in argv else 0, "secs": 0.0, "tail": ""}
    monkeypatch.setattr(up, "run_step", fake)
    a = args(cfg, "us")
    a.notify = True
    assert up.cmd_update(a) == 1
    assert len(seen) == 6                                                                           # 富途失败后公开行情/预测/对账照常执行
    assert notes and "失败 1 步" in notes[0][1]


def test_no_futu_flag_skips_broker_steps(env):
    cfg, calls, _ = env
    a = args(cfg, "us")
    a.no_futu = True
    assert up.cmd_update(a) == 0
    assert not any("futu" in c for c in calls)


def test_second_concurrent_run_is_skipped(env, tmp_path):
    cfg, calls, _ = env
    (tmp_path / "logs").mkdir()
    held = open(tmp_path / "logs" / "update.lock", "w")           # noqa: SIM115
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert up.cmd_update(args(cfg, "us")) == 0 and calls == []     # 已有更新在跑：本次跳过
    held.close()


def test_last_final_session_waits_for_close_plus_buffer():
    # 2026-10-05 周一：美股收盘 20:00Z；19:00Z 时最近已收盘交易日是上周五
    assert up.last_final_session("US", datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)).isoformat() == "2026-10-02"
    assert up.last_final_session("US", datetime(2026, 10, 5, 20, 40, tzinfo=timezone.utc)).isoformat() == "2026-10-05"
