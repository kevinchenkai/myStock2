"""例行更新命令：步骤编排、失败不中断、非零退出、锁、通知（子进程与行情/数据库全部替换为假实现）。"""
import argparse
import fcntl
from datetime import datetime, timezone

import pytest

from mystock2.cli import update as up

NOW = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)      # 周一 15:00 PDT：美股已收盘（20:00Z）2 小时、港股早已收盘


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"db:\n  path: {tmp_path / 'x.db'}\nweb:\n  host: 127.0.0.1\n  port: 8889\nfutu:\n  host: 127.0.0.1\n  port: 11111\n  trd_env: REAL\n"
                   "collect:\n  markets: [HK, US]\nupdate:\n  account_id: acct\n  acc_id: 7\n  lookback_days: 10\n", encoding="utf-8")
    monkeypatch.setattr(up, "LOG_DIR", tmp_path / "logs")
    calls = []
    monkeypatch.setattr(up, "codes_for", lambda cfg_, market, **k: ["US.NVDA", "US.TSLA"] if market == "US" else ["HK.00700"])
    monkeypatch.setattr(up, "_universe_codes", lambda: {"US.NVDA", "HK.00700"})
    monkeypatch.setattr(up, "freshness", lambda *a, **k: [])
    monkeypatch.setattr(up, "_now", lambda: NOW)
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    (tmp_path / "config" / "local").mkdir(parents=True)
    (tmp_path / "config" / "local" / "futu_cashflow_map.yaml").write_text("{}\n", encoding="utf-8")     # 有映射 → 有资金流水步骤
    notes = []
    monkeypatch.setattr(up, "notify", lambda t, x: notes.append((t, x)))

    def fake(argv, timeout=3600):
        calls.append(argv)
        return {"rc": 1 if "fail-me" in " ".join(argv) else 0, "secs": 0.0, "tail": ""}
    monkeypatch.setattr(up, "run_step", fake)
    return cfg, calls, notes


def args(cfg, phase, **kw):
    return argparse.Namespace(config=str(cfg), phase=phase, lookback=None, no_futu=False, notify=False, force=False, **kw)


def key(cfg, phase):
    from mystock2.core.config import load_config
    return up.state_key(phase, load_config(cfg))


def rec(cfg, phase):
    return up.load_state()[key(cfg, phase)]


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
    assert got == ["collect futu", "collect futu", "collect quotes", "collect quotes", "forecast run", "forecast run", "forecast run", "forecast run", "ledger reconcile"]
    futu = calls[0]
    assert futu[futu.index("--what") + 1] == "deals,orders,fees,snapshot" and "--assume-market-currency" in futu and futu[futu.index("--acc-id") + 1] == "7"
    assert calls[1][calls[1].index("--what") + 1] == "cashflow"
    daily = calls[2]
    assert daily[daily.index("--codes") + 1] == "US.NVDA,US.TSLA" and "--fx" in daily and "--hourly" in calls[3]
    assert [c[c.index("--model") + 1] for c in calls[4:6]] == ["baseline", "lgbm"] and "--tag" in calls[6] and calls[4][calls[4].index("--codes") + 1] == "US.NVDA"   # 预测只对名单内标的


def test_pre_phase_is_light_and_hk_phase_uses_hk_codes(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "pre")) == 0
    assert [c[c.index("--what") + 1] for c in calls if "--what" in c] == ["orders,snapshot"]                # 盘前：不拉成交/费用，不预测、不对账
    assert not any("forecast" in a or "reconcile" in a for c in calls for a in c)
    calls.clear()
    assert up.cmd_update(args(cfg, "hk")) == 0
    assert any("HK.00700" in a for c in calls for a in c) and not any("US.NVDA" in a for c in calls for a in c)


def test_hk_and_us_phases_also_write_forward_forecast_for_last_closed_session_only(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "hk")) == 0
    fwd = [c for c in calls if "--tag" in c]
    assert [c[c.index("--model") + 1] for c in fwd] == ["baseline", "lgbm"] and all(c[c.index("--tag") + 1] == "forward" for c in fwd)
    assert all(c[c.index("--start") + 1] == c[c.index("--end") + 1] == "2026-10-05" and c[c.index("--codes") + 1] == "HK.00700" for c in fwd)   # 最近已收盘交易日，只对名单
    rebuilt = [c for c in calls if "forecast" in c and "--tag" not in c]
    assert len(rebuilt) == 2                                                                      # 原有 rebuilt 回补仍在
    calls.clear()
    assert up.cmd_update(args(cfg, "us")) == 0
    fwd_us = [c for c in calls if "--tag" in c]                                                   # 美股阶段同样写前向（最近已收盘的美股交易日）
    assert len(fwd_us) == 2 and all(c[c.index("--start") + 1] == c[c.index("--end") + 1] == "2026-10-05" for c in fwd_us)
    calls.clear()
    assert up.cmd_update(args(cfg, "pre")) == 0
    assert not any("--tag" in c for c in calls)                                                   # 盘前阶段不预测


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
    assert len(seen) == 9                                                                           # 富途失败后公开行情/预测/对账照常执行
    assert notes and "失败 2 步" in notes[0][1]


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


def test_incremental_check_skips_when_target_done_and_fresh_but_reruns_when_stale_failed_or_forced(env, monkeypatch):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "us")) == 0 and len(calls) == 9                 # 第一次：完整运行并记录成功
    calls.clear()
    assert up.cmd_update(args(cfg, "us")) == 0 and calls == []                       # 备份时间点：同一目标已完成 → 不连富途、不拉行情
    a = args(cfg, "us")
    a.force = True
    assert up.cmd_update(a) == 0 and len(calls) == 9                                 # --force 强制
    calls.clear()
    monkeypatch.setattr(up, "freshness", lambda *a_, **k: ["US.NVDA: 最新终值 2026-10-02 < 应有 2026-10-05"])
    assert up.cmd_update(args(cfg, "us")) == 1 and len(calls) == 9                   # 日线陈旧 → 不跳过（且本次仍陈旧 → 退出码 1）
    st = rec(cfg, "us")
    assert st["ok"] is False and st["target"] == "2026-10-05"
    monkeypatch.setattr(up, "freshness", lambda *a_, **k: [])
    calls.clear()
    assert up.cmd_update(args(cfg, "us")) == 0 and len(calls) == 9                   # 上次失败 → 备份时间点重试，成功后记录
    assert rec(cfg, "us")["ok"] is True


def test_success_recorded_too_soon_after_close_does_not_count(env):
    cfg, calls, _ = env
    st = {key(cfg, "us"): {"target": "2026-10-05", "at": "2026-10-05T20:10:00+00:00", "ok": True, "complete": True}}   # 收盘后 10 分钟的成功：成交/费用可能还没到齐
    up.save_state(st)
    assert up.cmd_update(args(cfg, "us")) == 0 and len(calls) == 9


def test_pre_phase_is_once_per_day(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "pre")) == 0 and calls
    calls.clear()
    assert up.cmd_update(args(cfg, "pre")) == 0 and calls == []



# ---------------------------------------------------------------- 审核 U-01～U-09
def test_u01_incomplete_success_is_not_a_reason_to_skip_later_runs(env):
    """--no-futu / 缺映射的成功不算「完整」：之后的时间点照常跑富途，而不是被增量检查跳过。"""
    cfg, calls, _ = env
    a = args(cfg, "us")
    a.no_futu = True
    assert up.cmd_update(a) == 0 and rec(cfg, "us")["complete"] is False
    calls.clear()
    assert up.cmd_update(args(cfg, "us")) == 0 and any("futu" in c for c in calls)


def test_u02_explicit_lookback_is_a_manual_backfill_and_is_validated(env):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "us")) == 0
    calls.clear()
    a = args(cfg, "us")
    a.lookback = 30
    assert up.cmd_update(a) == 0 and len(calls) == 9                                 # 不被「已完成」跳过
    hourly = next(c for c in calls if "--hourly" in c)
    assert hourly[hourly.index("--start") + 1] == "2026-09-05"                        # 小时线跟随回看天数（U-07）
    calls.clear()
    a.lookback = 0
    assert up.cmd_update(a) == 1 and calls == []                                     # 0/负数：报错，不悄悄回退到 10


def test_u03_pre_target_is_the_next_us_session_in_new_york_time():
    # 周一 17:30 PDT（=周二 00:30Z）：下一个开盘的是周二 10-06
    assert up.target_key("pre", "US", datetime(2026, 10, 6, 0, 30, tzinfo=timezone.utc)) == "2026-10-06"
    # 周二 05:45 PDT（=12:45Z）盘前：仍是周二
    assert up.target_key("pre", "US", datetime(2026, 10, 6, 12, 45, tzinfo=timezone.utc)) == "2026-10-06"
    # 周五开盘后：下一个是下周一
    assert up.target_key("pre", "US", datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)) == "2026-10-12"


def test_u04_unexpected_errors_and_bad_state_files_are_logged_and_notified(env, monkeypatch):
    cfg, calls, notes = env
    (up.LOG_DIR).mkdir(parents=True, exist_ok=True)
    (up.LOG_DIR / "state.json").write_text("null", encoding="utf-8")
    assert up.cmd_update(args(cfg, "us")) == 0 and len(calls) == 9                  # 状态文件内容不对：当作没有记录
    up.save_state({key(cfg, "us"): {"target": "2026-10-05", "ok": True, "complete": True}})       # 缺 at
    calls.clear()
    assert up.cmd_update(args(cfg, "us")) == 0 and len(calls) == 9
    monkeypatch.setattr(up, "last_final_session", lambda *a_: (_ for _ in ()).throw(up.ConfigError("日历缺失")))
    a = args(cfg, "us")
    a.notify = True
    assert up.cmd_update(a) == 1 and notes and "异常中止" in notes[-1][1]
    assert list(up.LOG_DIR.glob("update_*_us_error.json"))


def test_u05_lock_holder_is_recorded_and_skips_are_logged(env, tmp_path):
    cfg, calls, _ = env
    lock, _ = up._acquire_lock("hk")
    assert up.cmd_update(args(cfg, "us")) == 0 and calls == []
    assert '"phase": "hk"' in (up.LOG_DIR / "update_lock_skipped.log").read_text(encoding="utf-8")
    lock.close()


def test_u09_state_is_per_database(env, tmp_path):
    cfg, calls, _ = env
    assert up.cmd_update(args(cfg, "us")) == 0
    other = tmp_path / "other.yaml"
    other.write_text(cfg.read_text(encoding="utf-8").replace("x.db", "y.db"), encoding="utf-8")
    calls.clear()
    assert up.cmd_update(args(other, "us")) == 0 and len(calls) == 9                 # 另一个库：不因前一个库已完成而跳过


def test_u06_codes_are_universe_current_holdings_and_recent_trades_not_all_history(tmp_path, monkeypatch):
    from datetime import date

    from mystock2.core import db as dbmod
    from mystock2.core.config import parse_config
    from mystock2.ledger.events import EventDraft, ensure_account, fill_key, post_event
    from mystock2.ledger.opening import record_opening

    p = tmp_path / "x.db"
    dbmod.migrate(p)
    w = dbmod.connect_writer(p, "ledger")
    ensure_account(w, "acct", "futu", "REAL", "USD")
    record_opening(w, "acct", "2024-01-01T00:00:00Z", {"US.HELD": "10"}, {"USD": "0"})
    for deal, code, q, at in (("a", "US.GONE", "5", "2024-02-01T15:00:00Z"), ("b", "US.GONE", "-5", "2024-03-01T15:00:00Z"),
                              ("c", "US.RECENT", "1", "2026-10-01T15:00:00Z"), ("d", "US.RECENT", "-1", "2026-10-02T15:00:00Z")):
        post_event(w, EventDraft(fill_key("acct", deal), "acct", "FILL", at, "USD", code=code, price="10", qty_delta=q, cash_delta=str(-10 * int(q)), ref_deal_id=deal))
    w.close()
    cfg = parse_config({"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["US"]}, "db": {"path": str(p)},
                        "web": {"host": "127.0.0.1", "port": 8889}}, base=tmp_path)
    monkeypatch.setattr(up, "_universe_codes", lambda: {"US.NVDA", "HK.00700"})
    assert up.codes_for(cfg, "US", account="acct", since=date(2026, 9, 20)) == ["US.HELD", "US.NVDA", "US.RECENT"]       # 不含早已清仓的 US.GONE
