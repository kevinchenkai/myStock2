import sqlite3

import pytest

from mystock2.core import db as dbmod
from mystock2.core.runs import run_log


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    dbmod.migrate(path)
    return path


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    latest = [m.version for m in dbmod.discover_migrations()]
    first = dbmod.migrate(path)
    assert first == latest
    assert dbmod.migrate(path) == []          # 重复迁移幂等
    assert dbmod.schema_version(path) == latest[-1]


def test_every_table_has_an_owner(db):
    conn = dbmod.connect_ro(db)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables <= set(dbmod.TABLE_OWNERS), f"未登记写入者的表：{tables - set(dbmod.TABLE_OWNERS)}"


def test_tampering_with_applied_migration_is_detected(tmp_path):
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("CREATE TABLE schema_note (x INTEGER);\n", encoding="utf-8")
    path = tmp_path / "t.db"
    dbmod.migrate(path, mig)
    (mig / "0001_a.sql").write_text("CREATE TABLE schema_note (x INTEGER, y INTEGER);\n", encoding="utf-8")
    with pytest.raises(dbmod.DbError, match="校验和"):
        dbmod.migrate(path, mig)


def test_failed_migration_rolls_back(tmp_path):
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_bad.sql").write_text("CREATE TABLE ok_t (x INTEGER);\nINSERT INTO nope VALUES (1);\n", encoding="utf-8")
    path = tmp_path / "t.db"
    with pytest.raises(sqlite3.OperationalError):
        dbmod.migrate(path, mig)
    conn = dbmod.connect_ro(path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok_t" not in names                 # 整个迁移回滚
    assert dbmod.schema_version(path) == 0


def test_migration_numbering_gaps_rejected(tmp_path):
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (mig / "0003_c.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(dbmod.DbError, match="不连续"):
        dbmod.discover_migrations(mig)


def test_readonly_connection_rejects_writes(db):
    conn = dbmod.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO instrument(code, market, yf_symbol, currency) VALUES ('US.X','US','X','USD')")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE evil (x)")


def test_ro_requires_existing_db(tmp_path):
    with pytest.raises(dbmod.DbError):
        dbmod.connect_ro(tmp_path / "missing.db")


def test_writer_can_write_only_owned_tables(db):
    inst = dbmod.connect_writer(db, "instruments")
    inst.execute("INSERT INTO instrument(code, market, yf_symbol, currency) VALUES ('US.NVDA','US','NVDA','USD')")
    with pytest.raises(sqlite3.DatabaseError):                      # 不属于 instruments 的表
        inst.execute("INSERT INTO run_log(run_id, command, started_at, status) VALUES ('x','c','2026-01-01T00:00:00Z','ok')")
    with pytest.raises(sqlite3.DatabaseError):                      # 迁移账本表
        inst.execute("DELETE FROM schema_migration")
    with pytest.raises(sqlite3.DatabaseError):                      # DDL
        inst.execute("CREATE TABLE evil (x)")
    with pytest.raises(sqlite3.DatabaseError):
        inst.execute("DROP TABLE instrument")
    with pytest.raises(sqlite3.DatabaseError):
        inst.execute("UPDATE run_log SET status='ok'")


def test_unregistered_owner_and_migrator_owner_rejected(db):
    with pytest.raises(dbmod.DbError):
        dbmod.connect_writer(db, "assistant")          # 未登记写入者（如尚未落地的模块）一律拒绝
    with pytest.raises(dbmod.DbError):
        dbmod.connect_writer(db, dbmod.OWNER_MIGRATOR)


def test_unregistered_table_is_write_denied_for_everyone(tmp_path):
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("CREATE TABLE stray (x INTEGER);\n", encoding="utf-8")
    path = tmp_path / "t.db"
    dbmod.migrate(path, mig)
    for owner in ("core", "instruments"):
        conn = dbmod.connect_writer(path, owner)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO stray VALUES (1)")        # 失败关闭：未登记的表不可写


def test_run_log_success_failure_and_partial(db):
    conn = dbmod.connect_writer(db, "core")
    with run_log(conn, "demo", {"k": "v"}) as run:
        run.note(rows=3)
    with pytest.raises(RuntimeError):
        with run_log(conn, "boom"):
            raise RuntimeError("x")
    with run_log(conn, "half") as run:
        run.partial("retry HK only", failed=["HK.00700"])
    rows = {r["command"]: r for r in conn.execute("SELECT * FROM run_log")}
    assert rows["demo"]["status"] == "ok" and rows["demo"]["finished_at"]
    assert rows["boom"]["status"] == "failed" and "RuntimeError" in rows["boom"]["detail_json"]
    assert rows["half"]["status"] == "partial" and rows["half"]["retry_scope"] == "retry HK only"
    assert rows["demo"]["run_id"].startswith("r_")
