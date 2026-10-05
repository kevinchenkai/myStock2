"""V1 只读导入（合成 V1 库；DDL 取自 V1 `mystock/schema.sql` 的相关表，仅用于测试）。"""
import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from mystock2.collectors.v1_import import V1Error, open_v1_readonly, run_import
from mystock2.core import db as dbmod
from mystock2.ledger import opening
from mystock2.ledger.events import EventDraft, fill_key, open_pending, post_event
from mystock2.ledger.projection import project

V1_DDL = """
CREATE TABLE positions (snapshot_date TEXT NOT NULL, market TEXT NOT NULL, code TEXT NOT NULL, name TEXT, qty REAL, can_sell_qty REAL, cost_price REAL,
  nominal_price REAL, market_val REAL, pl_val REAL, pl_ratio REAL, currency TEXT, updated_at TEXT, PRIMARY KEY (snapshot_date, market, code));
CREATE TABLE orders (order_id TEXT PRIMARY KEY, market TEXT, code TEXT, name TEXT, trd_side TEXT, order_type TEXT, order_status TEXT, price REAL, qty REAL,
  dealt_qty REAL, dealt_avg_price REAL, create_time TEXT, updated_time TEXT, currency TEXT, raw_json TEXT, synced_at TEXT);
CREATE TABLE deals (deal_id TEXT PRIMARY KEY, order_id TEXT, market TEXT, code TEXT, name TEXT, trd_side TEXT, price REAL, qty REAL, create_time TEXT,
  counter_broker_id TEXT, raw_json TEXT, synced_at TEXT);
CREATE TABLE account_funds (snapshot_date TEXT PRIMARY KEY, report_currency TEXT, total_assets REAL, market_val REAL, cash REAL, frozen_cash REAL,
  avl_withdrawal_cash REAL, power REAL, hkd_assets REAL, hk_cash REAL, usd_assets REAL, us_cash REAL, risk_status TEXT, updated_at TEXT);
"""


@pytest.fixture()
def v1_path(tmp_path):
    p = tmp_path / "v1.db"
    c = sqlite3.connect(p)
    c.executescript(V1_DDL)
    deals = [
        ("D1", "O1", "US", "US.NVDA", "BUY", 123.45000457763672, 10.0, "2026-03-03 10:30:00"),         # 美股本地（东部）时间
        ("D2", "O2", "HK", "HK.00700", "BUY", 428.2, 100.0, "2026-03-03 10:00:00.123"),                 # 港股本地时间，带毫秒
        ("D3", "O3", "US", "US.NVDA", "SELL", 130.0, 4.0, "2026-03-05 11:00:00"),
        ("D4", "O4", "US", "US.TSLA", "BUY", 200.0, 1.0, None),                                          # 缺时间：跳过并报告
        (None, "O5", "US", "US.TSLA", "BUY", 200.0, 1.0, "2026-03-04 10:00:00"),                         # 缺 deal_id：进待匹配
        ("D6", "O6", "XX", "SH.600000", "BUY", 1.0, 1.0, "2026-03-04 10:00:00"),                         # 非 HK/US 代码：跳过
    ]
    c.executemany("INSERT INTO deals(deal_id, order_id, market, code, trd_side, price, qty, create_time) VALUES (?,?,?,?,?,?,?,?)", deals)
    c.execute("INSERT INTO orders(order_id, market, code, trd_side, order_status, price, qty, dealt_qty) VALUES ('OC','US','US.NVDA','BUY','CANCELLED_ALL',100,5,0)")
    c.execute("INSERT INTO positions(snapshot_date, market, code, qty, can_sell_qty, cost_price) VALUES ('2026-03-05','US','US.NVDA',6,6,124.1234)")
    c.execute("INSERT INTO account_funds(snapshot_date, hk_cash, us_cash) VALUES ('2026-03-05', 1000.5, 20000.25)")
    c.commit()
    c.close()
    return p


@pytest.fixture()
def led(tmp_path):
    p = tmp_path / "v2.db"
    dbmod.migrate(p)
    return dbmod.connect_writer(p, "ledger")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_import_is_read_only_maps_fills_and_reports_gaps(v1_path, led):
    before = sha(v1_path)
    rep = run_import(v1_path, led, account_id="LEGACY")
    assert sha(v1_path) == before                                          # V1 库字节不变
    assert (rep.deals_read, rep.inserted, rep.pending) == (6, 3, 1)
    assert sorted(x[0] for x in rep.skipped) == ["D4", "D6"] and rep.skipped[0][1].startswith(("incomplete", "bad_code"))
    ev = {r["ref_deal_id"]: r for r in led.execute("SELECT * FROM ledger_event WHERE event_type='FILL'")}
    assert ev["D1"]["price"] == "123.45" and ev["D1"]["qty_delta"] == "10" and ev["D1"]["cash_delta"] == "-1234.5"     # 量化到 4 位小数
    assert ev["D1"]["event_at"] == "2026-03-03T15:30:00.000000Z"                                                              # 美东 10:30（EST，UTC-5）→ UTC
    assert ev["D2"]["event_at"] == "2026-03-03T02:00:00.123000Z"                                                       # 港股 10:00 HKT → 02:00Z
    assert ev["D3"]["qty_delta"] == "-4" and ev["D3"]["cash_delta"] == "520"
    assert "tz_inferred" in ev["D1"]["note"] and rep.tz_inferred == 4 and rep.max_price_rounding > 0
    assert rep.total_notional_rounding < Decimal("0.01") and "fees_not_in_v1" in rep.gaps and "cash_flows_not_in_v1" in rep.gaps
    assert len(open_pending(led)) == 1                                                                                 # 缺 deal_id 不入账
    assert led.execute("SELECT COUNT(*) c FROM source_record WHERE source='v1'").fetchone()["c"] >= 4
    assert rep.snapshots == 1 and led.execute("SELECT source FROM account_snapshot").fetchone()["source"] == "v1-date-only"
    cash = {r["currency"]: r["cash"] for r in led.execute("SELECT * FROM snapshot_cash")}
    assert cash == {"HKD": "1000.5", "USD": "20000.25"}
    assert led.execute("SELECT cost_basis FROM snapshot_position").fetchone()["cost_basis"] == "124.1234"
    p = project(led, "LEGACY")
    assert p.positions == {"US.NVDA": Decimal(6), "HK.00700": Decimal(100)} and "no_opening" in p.warnings                # 无开账点：如实告警


def test_second_run_is_idempotent_and_dry_run_writes_nothing(v1_path, led):
    dry = run_import(v1_path, led, account_id="LEGACY", dry_run=True)
    assert dry.inserted == 3 and led.execute("SELECT COUNT(*) c FROM ledger_event").fetchone()["c"] == 0
    run_import(v1_path, led, account_id="LEGACY")
    again = run_import(v1_path, led, account_id="LEGACY")
    assert (again.inserted, again.duplicate) == (0, 3)
    assert led.execute("SELECT COUNT(*) c FROM ledger_event").fetchone()["c"] == 3


def test_cross_channel_dedup_with_a_futu_direct_fill_and_conflict_is_reported(v1_path, led):
    # 同一笔成交（deal D1）先由 Futu 直采入账（与 V1 同一规范键），再导入 V1：不重复，只补证据链接
    from mystock2.ledger.events import SourceDraft, ensure_account
    ensure_account(led, "LEGACY", "futu", "REAL")
    post_event(led, EventDraft(fill_key("LEGACY", "D1"), "LEGACY", "FILL", "2026-03-03T15:30:00.000000Z", "USD", code="US.NVDA", price="123.45", qty_delta="10",
                               cash_delta="-1234.5", ref_deal_id="D1", note="v1_import;tz_inferred;quantized(price4,qty6)"),
               source=SourceDraft("futu", "D1", {"deal_id": "D1"}))
    rep = run_import(v1_path, led, account_id="LEGACY")
    assert rep.duplicate == 1 and rep.inserted == 2
    assert led.execute("SELECT COUNT(*) c FROM ledger_event WHERE business_key=?", (fill_key("LEGACY", "D1"),)).fetchone()["c"] == 1
    assert led.execute("SELECT COUNT(*) c FROM source_link l JOIN ledger_event e ON e.event_id=l.event_id WHERE e.business_key=?", (fill_key("LEGACY", "D1"),)).fetchone()["c"] == 2
    # 内容冲突：不静默合并
    led2 = dbmod.connect_writer(led.execute("PRAGMA database_list").fetchone()["file"], "ledger")
    c = sqlite3.connect(v1_path)
    c.execute("UPDATE deals SET price=999.0 WHERE deal_id='D3'")
    c.commit()
    c.close()
    post_event(led2, EventDraft(fill_key("LEGACY", "D3"), "LEGACY", "FILL", "2026-03-05T16:00:00.000000Z", "USD", code="US.NVDA", price="130", qty_delta="-4", cash_delta="520",
                                ref_deal_id="D3"))
    rep2 = run_import(v1_path, led2, account_id="LEGACY")
    assert any("D3" in x for x in rep2.conflicts)


def test_post_opening_boundary_applies_to_imported_history(v1_path, led):
    run_import(v1_path, led, account_id="LEGACY")
    opening.record_opening(led, "LEGACY", "2026-03-04T00:00:00.000000Z", {"US.NVDA": "10", "HK.00700": "100"}, {"USD": "0"})
    p = project(led, "LEGACY")
    assert p.pre_opening_events == 2 and p.positions == {"US.NVDA": Decimal(6), "HK.00700": Decimal(100)}     # 开账前的 D1、D2 只作描述；D3 卖 4 股在开账后


def test_not_a_v1_db_and_missing_file_are_refused(tmp_path):
    with pytest.raises(V1Error, match="不存在"):
        open_v1_readonly(tmp_path / "nope.db")
    other = tmp_path / "other.db"
    sqlite3.connect(other).executescript("CREATE TABLE t (x)")
    with pytest.raises(V1Error, match="不像 V1"):
        open_v1_readonly(other)
    ro = open_v1_readonly(tmp_path / "other2.db") if False else None
    assert ro is None


def test_connection_cannot_write_to_v1(v1_path):
    ro = open_v1_readonly(v1_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO deals(deal_id) VALUES ('X')")


def test_ledger_cli_open_reconcile_and_status(tmp_path):
    import subprocess
    import sys

    import yaml

    from mystock2.core.config import REPO_ROOT
    from mystock2.ledger.events import ensure_account
    db = tmp_path / "l.db"
    dbmod.migrate(db)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}, "collect": {"markets": ["US"]}, "db": {"path": str(db)},
                                   "web": {"host": "127.0.0.1", "port": 8889}}), encoding="utf-8")
    led = dbmod.connect_writer(db, "ledger")
    ensure_account(led, "A1", "futu", "REAL")
    sid = opening.create_snapshot(led, "A1", "2026-03-02T21:00:00.000000Z", "futu", {"US.NVDA": {"qty": "10"}}, {"USD": {"cash": "1000"}})
    led.close()

    def run(*a):
        return subprocess.run([sys.executable, "-m", "mystock2", "--config", str(cfg), *a], capture_output=True, text=True, cwd=REPO_ROOT)
    r = run("ledger", "open", "--account-id", "A1")
    assert r.returncode == 0 and "opening_at=2026-03-02T21:00:00.000000Z" in r.stdout
    st = run("ledger", "status", "--account-id", "A1")
    assert '"US.NVDA": "10"' in st.stdout and '"USD": "1000"' in st.stdout
    led = dbmod.connect_writer(db, "ledger")
    post_event(led, EventDraft(fill_key("A1", "N1"), "A1", "FILL", "2026-03-03T15:00:00.000000Z", "USD", code="US.NVDA", price="100", qty_delta="1", cash_delta="-100", ref_deal_id="N1"))
    led.close()
    rec = run("ledger", "reconcile", "--account-id", "A1", "--snapshot", sid)
    assert rec.returncode == 0 and '"ok": true' in rec.stdout                     # 快照在成交之前：对账只看快照时点之前的事件
    assert run("ledger", "reconcile", "--account-id", "A1", "--snapshot", "nope").returncode == 2
