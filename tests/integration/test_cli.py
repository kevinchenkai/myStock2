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
