import subprocess
import sys

import yaml

from mystock2.core import db as dbmod
from mystock2.core.config import REPO_ROOT


def run(*args, cfg=None):
    cmd = [sys.executable, "-m", "mystock2", *(["--config", str(cfg)] if cfg else []), *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)


def make_cfg(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({
        "futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"},
        "collect": {"markets": ["US"]},
        "db": {"path": str(tmp_path / "x.db")},
        "web": {"host": "127.0.0.1", "port": 8889},
    }), encoding="utf-8")
    return cfg


def test_help_and_version():
    assert "myStock2" in run("--help").stdout
    assert run("version").stdout.strip() == "0.1.0"


def test_migrate_twice_with_run_receipts(tmp_path):
    cfg = make_cfg(tmp_path)
    a = run("db", "migrate", cfg=cfg)
    latest = [m.version for m in dbmod.discover_migrations()]
    assert a.returncode == 0 and f"applied={latest}" in a.stdout and "run_id=r_" in a.stdout
    b = run("db", "migrate", cfg=cfg)
    assert b.returncode == 0 and "applied=[]" in b.stdout
    assert f"schema_version={latest[-1]}" in run("db", "status", cfg=cfg).stdout


def test_universe_check_template_and_bare_code(tmp_path):
    ok = run("universe", "check", "--file", str(REPO_ROOT / "config" / "universe.example.yaml"))
    assert ok.returncode == 0 and "US.NVDA" in ok.stdout
    bad = tmp_path / "u.yaml"
    bad.write_text(yaml.safe_dump({"instruments": [{"code": "NV", "tier": "trade"}]}), encoding="utf-8")
    r = run("universe", "check", "--file", str(bad), "--known", "US.NVDA")
    assert r.returncode == 2 and "US.NVDA" in r.stderr


def test_missing_config_file_gives_clean_error(tmp_path):
    r = run("config-show", cfg=tmp_path / "nope.yaml")
    assert r.returncode == 1 and "配置文件不存在" in r.stderr


def test_forecast_run_forward_tag_is_refused_for_a_historical_range(tmp_path):
    """审核 P1-1：历史区间只能是 rebuilt；--tag forward 只允许单日（再由写入层校验生成时间）。"""
    cfg = make_cfg(tmp_path)
    run("db", "migrate", cfg=cfg)
    r = run("forecast", "run", "--start", "2025-03-03", "--end", "2025-03-07", "--codes", "US.NVDA", "--tag", "forward", cfg=cfg)
    assert r.returncode == 2 and "rebuilt" in r.stderr


def test_ledger_open_skips_flat_snapshot_lines_and_reconcile_refuses_float_baseline(tmp_path):
    """审核 P3：快照里 qty=0 的行（已清仓）不让开账失败；known_cash_diffs 写成不带引号的小数（float）给出明确错误，小写币种也生效。"""
    from mystock2.ledger.events import ensure_account
    from mystock2.ledger.opening import create_snapshot

    cfg = make_cfg(tmp_path)
    run("db", "migrate", cfg=cfg)
    w = dbmod.connect_writer(tmp_path / "x.db", "ledger")
    ensure_account(w, "main", "futu", "REAL", "USD")
    create_snapshot(w, "main", "2026-03-02T00:00:00Z", "futu", {"US.NVDA": {"qty": "10"}, "US.TSLA": {"qty": "0"}}, {"USD": {"cash": "100"}})
    w.close()
    r = run("ledger", "open", "--account-id", "main", cfg=cfg)
    assert r.returncode == 0 and "positions=1" in r.stdout, r.stderr
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["reconcile"] = {"known_cash_diffs": {"usd": -13.84}}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    r = run("ledger", "reconcile", "--account-id", "main", cfg=cfg)
    assert r.returncode == 1 and "带引号" in r.stderr
    data["reconcile"] = {"known_cash_diffs": {"usd": "0"}}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert run("ledger", "reconcile", "--account-id", "main", cfg=cfg).returncode == 0
