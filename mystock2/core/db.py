"""SQLite 连接、写表授权与迁移器（实施方案 §3.4、WP1.4）。

三类连接：
- `connect_ro`：URI `mode=ro` + `query_only`，Web 与研究读取使用；写操作被 SQLite 拒绝。
- `connect_writer(owner)`：读写连接，装有 `set_authorizer` 写表授权器——只允许写 `TABLE_OWNERS` 中归属于 `owner` 的表，
  其余表（包括未登记的表）的 INSERT/UPDATE/DELETE 与全部 DDL 一律拒绝（失败关闭）。
- `connect_migrator`：仅迁移器使用，可执行 DDL；不用于业务写入。

import 图不足以保证「研究不改账户事实」，所以写权限在连接层强制（有测试）。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mystock2.core.timeutil import iso_utc, utc_now

# ---- 写表登记：表 -> 所属写入者。后续里程碑新增表时在这里登记（测试会检查每张表都有归属）。
OWNER_MIGRATOR = "migrator"
TABLE_OWNERS: dict[str, str] = {
    "schema_migration": OWNER_MIGRATOR,
    "run_log": "core",
    "instrument": "instruments",
    # ledger（M2a）
    "account": "ledger",
    "account_opening": "ledger",
    "source_record": "ledger",
    "ledger_event": "ledger",
    "source_link": "ledger",
    "pending_match": "ledger",
    "pending_resolution": "ledger",
    "corporate_action": "ledger",
    "fee_profile": "ledger",
    "account_snapshot": "ledger",
    "snapshot_position": "ledger",
    "snapshot_cash": "ledger",
    "broker_order": "ledger",
    "instrument_name": "ledger",
    "instrument_profile": "market",
    "quote_preopen": "market",
    "capital_flow_daily": "market",
    "v1_prediction_archive": "forecast",
    # market / forecast（M4）
    "quote_daily": "market",
    "quote_hourly": "market",
    "fx_rate": "market",
    "collection_log": "market",
    "evidence_snapshot": "market",
    "security_rule": "instruments",
    "prediction_version": "forecast",
    # scoreboard（M5）
    "comparison_batch": "scoreboard",
    "strategy_line": "scoreboard",
    "eval_run": "scoreboard",
    "sleeve_daily": "scoreboard",
    "line_state": "scoreboard",
    "sim_fill": "scoreboard",
    "run_cost": "scoreboard",
    # coach（M6）
    "ticket": "coach",
    "intent": "coach",
    "intent_exposure": "coach",
    "protocol_freeze": "coach",
    "ticket_group": "coach",
    # assistant（M9）
    "veto_packet": "assistant",
    "llm_call": "assistant",
}

# 复合写入者：需要在**同一事务**里写多个所有者的表时使用（如 veto 导入：票据 + 调用回执）。
OWNER_GROUPS: dict[str, tuple[str, ...]] = {"veto": ("assistant", "coach")}

_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
# DDL 与其他会改变库结构的动作
_DDL_ACTIONS = {
    getattr(sqlite3, n)
    for n in dir(sqlite3)
    if n.startswith(("SQLITE_CREATE_", "SQLITE_DROP_", "SQLITE_ALTER_"))
} | {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_REINDEX}


class DbError(RuntimeError):
    pass


def _authorizer_for(owners: tuple[str, ...]):
    def authorizer(action, arg1, arg2, dbname, source):
        if action in _DDL_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action in _WRITE_ACTIONS:
            if TABLE_OWNERS.get(arg1) in owners:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


def _base_connect(path: str | Path, *, uri_mode: str | None = None) -> sqlite3.Connection:
    p = str(path)
    if uri_mode:
        conn = sqlite3.connect(f"file:{p}?mode={uri_mode}", uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(p, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """只读连接（Web 使用）。库文件必须已存在。"""
    if not Path(path).exists():
        raise DbError(f"数据库不存在：{path}")
    conn = _base_connect(path, uri_mode="ro")
    conn.execute("PRAGMA query_only = ON")
    return conn


def connect_writer(path: str | Path, owner: str) -> sqlite3.Connection:
    """带写表授权器的读写连接。owner 必须是 TABLE_OWNERS 中出现过的写入者名。"""
    if owner == OWNER_MIGRATOR:
        raise DbError("迁移器连接请使用 connect_migrator")
    owners = OWNER_GROUPS.get(owner, (owner,))
    if not set(owners) <= set(TABLE_OWNERS.values()):
        raise DbError(f"未登记的写入者：{owner!r}")
    if not Path(path).exists():
        raise DbError(f"数据库不存在（请先 `db migrate`）：{path}")
    conn = _base_connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.set_authorizer(_authorizer_for(owners))
    return conn


def connect_migrator(path: str | Path) -> sqlite3.Connection:
    """迁移器连接：可创建库、执行 DDL。仅 `migrate()` 使用。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _base_connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """事务/保存点：最外层 BEGIN IMMEDIATE，嵌套用 SAVEPOINT；异常整体回滚。"""
    outer = not conn.in_transaction
    sp = "sp_" + secrets.token_hex(4)
    conn.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {sp}")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK" if outer else f"ROLLBACK TO {sp}")
        if not outer:
            conn.execute(f"RELEASE {sp}")
        raise
    else:
        conn.execute("COMMIT" if outer else f"RELEASE {sp}")


# ---------------------------------------------------------------- 迁移器

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    d = directory or DEFAULT_MIGRATIONS_DIR
    found = []
    for p in sorted(d.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(p.name[:4])
        found.append(Migration(version, p.stem[5:], p, p.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != sorted(set(versions)):
        raise DbError(f"迁移版本号重复或乱序：{versions}")
    if versions and versions != list(range(versions[0], versions[0] + len(versions))):
        raise DbError(f"迁移版本号不连续：{versions}")
    return found


def _split_statements(sql: str) -> list[str]:
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf)
            buf = ""
    if buf.strip():
        raise DbError("迁移文件末尾存在不完整的 SQL 语句")
    return statements


def migrate(db_path: str | Path, directory: Path | None = None) -> list[int]:
    """应用尚未应用的迁移，返回本次新应用的版本号。已应用迁移的内容被改动则报错（只前进，不改旧迁移）。"""
    migrations = discover_migrations(directory)
    conn = connect_migrator(db_path)
    applied_now: list[int] = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        done = {r["version"]: r for r in conn.execute("SELECT version, name, checksum FROM schema_migration")}
        for m in migrations:
            if m.version in done:
                if done[m.version]["checksum"] != m.checksum:
                    raise DbError(f"已应用的迁移 {m.version:04d}_{m.name} 内容被改动（校验和不一致）；迁移只前进，请新增迁移")
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_statements(m.sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migration(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                    (m.version, m.name, m.checksum, iso_utc(utc_now())),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied_now.append(m.version)
        return applied_now
    finally:
        conn.close()


def schema_version(db_path: str | Path) -> int:
    conn = connect_ro(db_path)
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migration").fetchone()
        return int(row["v"] or 0)
    finally:
        conn.close()
