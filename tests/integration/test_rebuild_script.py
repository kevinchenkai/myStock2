"""scripts/rebuild_real_db.sh 的安全闸（审核 P1-13）：先校验再删库、须确认、按配置的库路径、先备份、有前向记录拒绝、与例行更新互斥。
在临时目录里运行脚本副本，mystock2 命令由桩替代（不碰真实库、不联网）。"""
import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_real_db.sh"


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "rebuild_real_db.sh")
    (tmp_path / "config" / "local").mkdir(parents=True)
    (tmp_path / "config" / "local" / "futu_cashflow_map.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "real.db"
    sqlite3.connect(db).executescript("CREATE TABLE ledger_event(x); INSERT INTO ledger_event VALUES (1);")
    (tmp_path / "v1.db").write_text("v1", encoding="utf-8")
    stub = tmp_path / "py.sh"
    stub.write_text(f"""#!/bin/bash
if [ "$1" = "-m" ]; then
  echo "$*" >> "{tmp_path}/calls.log"
  if [ "$3" = "config-show" ]; then printf '{{"db_path": "%s", "is_example": %s}}\n' "{db}" "${{STUB_EXAMPLE:-false}}"; fi
  exit 0
fi
exec "{sys.executable}" "$@"
""", encoding="utf-8")
    stub.chmod(0o755)
    return tmp_path, db


def run(repo, **env):
    root, _ = repo
    e = {**os.environ, "PY": str(root / "py.sh"), "ACC_ID": "0", "V1_DB": str(root / "v1.db"), "OPEN_AT": "2024-10-20T00:00:00Z", **env}
    return subprocess.run(["bash", str(root / "scripts" / "rebuild_real_db.sh")], capture_output=True, text=True, env=e, cwd=root)


def test_refuses_before_touching_the_db_when_inputs_are_bad_or_unconfirmed(repo):
    root, db = repo
    assert run(repo, V1_DB=str(root / "missing.db"), CONFIRM="yes").returncode == 2 and db.exists()
    assert run(repo).returncode == 2 and db.exists()                                     # 未确认
    assert run(repo, CONFIRM="yes", STUB_EXAMPLE="true").returncode == 2 and db.exists()  # 配置回退到 example
    assert not list((root / "backups").glob("*.db")) if (root / "backups").exists() else True


def test_backs_up_then_rebuilds_the_configured_db(repo):
    root, db = repo
    r = run(repo, CONFIRM="yes")
    assert r.returncode == 0, r.stderr
    backups = list((root / "backups").glob("mystock2_before_rebuild_*.db"))
    assert len(backups) == 1 and sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0] == 1
    assert not db.exists()                                                               # 桩里的 migrate 不建库：删的正是配置里的路径
    calls = (root / "calls.log").read_text(encoding="utf-8")
    assert "db migrate" in calls and "ledger open --account-id main --at 2024-10-20T00:00:00Z" in calls


def test_forward_records_block_the_rebuild(repo):
    _, db = repo
    sqlite3.connect(db).executescript("CREATE TABLE ticket(x); INSERT INTO ticket VALUES (1);")
    r = run(repo, CONFIRM="yes")
    assert r.returncode == 2 and "ticket" in r.stderr and db.exists()


def test_refuses_while_the_daily_update_holds_the_lock(repo):
    root, db = repo
    (root / "data" / "logs").mkdir(parents=True)
    with open(root / "data" / "logs" / "update.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run(repo, CONFIRM="yes")
    assert r.returncode == 2 and "例行更新" in r.stderr and db.exists()
