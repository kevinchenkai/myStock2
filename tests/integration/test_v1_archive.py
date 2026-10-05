"""V1 其余数据迁移（合成 V1 库与合成 ML 库）：订单、名称、档案、资金流向、小时线、盘前价、V1 前向预测；以及富途订单采集。"""
import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from mystock2.collectors import v1_archive as va
from mystock2.collectors.futu import collect_orders, upsert_names
from mystock2.core import db as dbmod

V1_DDL = """
CREATE TABLE orders (order_id TEXT PRIMARY KEY, market TEXT, code TEXT, name TEXT, trd_side TEXT, order_type TEXT, order_status TEXT, price REAL, qty REAL,
  dealt_qty REAL, dealt_avg_price REAL, create_time TEXT, updated_time TEXT, currency TEXT, raw_json TEXT, synced_at TEXT);
CREATE TABLE deals (deal_id TEXT PRIMARY KEY, order_id TEXT, market TEXT, code TEXT, name TEXT, trd_side TEXT, price REAL, qty REAL, create_time TEXT);
CREATE TABLE positions (snapshot_date TEXT, market TEXT, code TEXT, name TEXT, qty REAL);
CREATE TABLE stock_profiles (futu_code TEXT PRIMARY KEY, yf_symbol TEXT, long_name TEXT, sector TEXT, industry TEXT, exchange TEXT, market_cap_mm REAL, shares_mm REAL,
  trailing_pe REAL, forward_pe REAL, price_to_book REAL, trailing_eps REAL, dividend_yield REAL, beta REAL, currency TEXT, website TEXT, synced_at TEXT,
  week52_high REAL, week52_low REAL, snap_synced_at TEXT, lot_size INTEGER);
CREATE TABLE capital_flow (code TEXT, date TEXT, in_flow REAL, main_in_flow REAL, super_in_flow REAL, big_in_flow REAL, mid_in_flow REAL, sml_in_flow REAL, synced_at TEXT);
"""
ML_DDL = """
CREATE TABLE ml_quotes_1h (symbol TEXT, futu_code TEXT, ts_utc TEXT, ts_et TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, synced_at TEXT, data_source TEXT, source_ref TEXT);
CREATE TABLE ml_preopen_quotes (code TEXT, date TEXT, price REAL, prev_close REAL, available_at TEXT, source TEXT, source_ref TEXT, synced_at TEXT);
CREATE TABLE ml_prediction_versions (prediction_id TEXT, run_id TEXT, code TEXT, as_of TEXT, target_session TEXT, source TEXT, status TEXT, generated_at TEXT,
  decision_at TEXT, published_at TEXT, manifest_path TEXT, payload_json TEXT, content_hash TEXT);
"""


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def v1(tmp_path):
    p = tmp_path / "v1.db"
    c = sqlite3.connect(p)
    c.executescript(V1_DDL)
    c.execute("INSERT INTO orders VALUES ('O1','HK','HK.00700','腾讯控股','BUY','NORMAL','CANCELLED_ALL',500,100,0,0,'2026-03-03 13:00:39.918','2026-03-03 16:21:38.051','HKD','{}','x')")
    c.execute("INSERT INTO orders VALUES ('O2','US','US.NVDA','英伟达','SELL','NORMAL','FILLED_ALL',130,4,4,130,'2026-03-05 11:00:00','2026-03-05 11:00:05','USD','{}','x')")
    c.execute("INSERT INTO orders VALUES ('O3','US','US.NVDA','英伟达','HOLD','NORMAL','FAILED',1,1,0,0,'2026-03-05 11:00:00',NULL,'USD','{}','x')")        # 方向非法：跳过
    c.execute("INSERT INTO deals(deal_id, code, name) VALUES ('D1','US.TSLA','特斯拉')")
    c.execute("INSERT INTO positions VALUES ('2026-03-05','US','US.NVDA','英伟达（持仓表）',6)")
    c.execute("INSERT INTO stock_profiles(futu_code, long_name, sector, industry, exchange, market_cap_mm, trailing_pe, currency, snap_synced_at, week52_high, week52_low, lot_size) "
              "VALUES ('US.NVDA','NVIDIA Corp','Technology','Semiconductors','NMS',4500000.5,41.2,'USD','2026-10-05T14:29:01+00:00',250.5,100.25,1)")
    c.execute("INSERT INTO capital_flow VALUES ('US.NVDA','2026-03-05',1.5e6,2.5e6,1e6,1.5e6,-1e5,2e5,'2026-03-06 01:00:00')")
    c.commit()
    c.close()
    return p


@pytest.fixture()
def ml(tmp_path):
    p = tmp_path / "ml.db"
    c = sqlite3.connect(p)
    c.executescript(ML_DDL)
    c.execute("INSERT INTO ml_quotes_1h VALUES ('NVDA','US.NVDA','2026-03-05 14:30:00','2026-03-05 09:30:00',100.0,101.5,99.5,101.0,1000,'x','yfinance',NULL)")
    c.execute("INSERT INTO ml_quotes_1h VALUES ('NVDA','US.NVDA','2026-03-05 20:30:00','2026-03-05 15:30:00',101.0,102.0,100.5,101.8,500,'x','yfinance',NULL)")     # 最后一根 30 分钟
    c.execute("INSERT INTO ml_quotes_1h VALUES ('NVDA','US.NVDA','2026-03-05 15:30:00','2026-03-05 10:30:00',101.0,100.0,102.0,101.0,10,'x','yfinance',NULL)")      # OHLC 自相矛盾：丢
    c.execute("INSERT INTO ml_preopen_quotes VALUES ('US.NVDA','2026-03-05',100.5,99.0,'2026-03-05T13:00:00+00:00','yfinance_1h',NULL,'2026-03-05 13:01:00')")
    c.execute("INSERT INTO ml_prediction_versions VALUES ('P1','R1','US.NVDA','2026-03-04','2026-03-05','live','published','2026-03-04 22:00:00',NULL,'2026-03-04 22:05:00',NULL,'{\"l_hat\":1}','h')")
    c.commit()
    c.close()
    return p


@pytest.fixture()
def dbs(tmp_path):
    p = tmp_path / "v2.db"
    dbmod.migrate(p)
    return dbmod.connect_writer(p, "ledger"), dbmod.connect_writer(p, "market"), dbmod.connect_writer(p, "forecast")


def test_archive_import_is_read_only_and_maps_everything(v1, ml, dbs):
    lw, mw, fw = dbs
    b1, b2 = sha(v1), sha(ml)
    v, m = va.open_ro(v1), va.open_ro(ml)
    assert va.import_names(v, lw) == 3
    assert va.import_orders(v, lw, account_id="A") == {"read": 3, "inserted": 2, "updated": 0, "skipped": ["O3"]}
    assert va.import_profiles(v, mw) == 1 and va.import_capital_flow(v, mw) == 1
    h = va.import_hourly(m, mw)
    assert (h["read"], h["inserted"], h["skipped_invalid"]) == (3, 2, 1)
    assert va.import_preopen(m, mw) == 1 and va.import_predictions(m, fw) == 1
    assert mw.execute("SELECT available_at FROM quote_preopen").fetchone()[0] == "2026-03-05T13:00:00.000000Z"      # 带时区的文本（真实 V1 数据格式）
    v.close()
    m.close()
    assert (sha(v1), sha(ml)) == (b1, b2)                                                      # V1 文件字节不变
    o = {r["order_id"]: dict(r) for r in lw.execute("SELECT * FROM broker_order")}
    assert o["O1"]["created_at"] == "2026-03-03T05:00:39.918000Z" and o["O1"]["status"] == "CANCELLED_ALL" and o["O1"]["time_trust"] == "assumed_local_tz"   # 港股本地时间 → UTC
    assert o["O2"]["created_at"] == "2026-03-05T16:00:00.000000Z" and o["O2"]["side"] == "SELL"
    names = {r["code"]: r["name"] for r in lw.execute("SELECT code, name FROM instrument_name")}
    assert names == {"HK.00700": "腾讯控股", "US.NVDA": "英伟达（持仓表）", "US.TSLA": "特斯拉"}      # 持仓表最晚，优先于订单/成交里的名称
    prof = dict(mw.execute("SELECT * FROM instrument_profile WHERE code='US.NVDA'").fetchone())
    assert prof["long_name"] == "NVIDIA Corp" and prof["week52_high"] == "250.5" and prof["source"] == "v1"
    bars = [dict(r) for r in mw.execute("SELECT bar_start, bar_end, open, source FROM quote_hourly ORDER BY bar_start")]
    assert bars[0]["bar_start"] == "2026-03-05T14:30:00.000000Z" and bars[0]["bar_end"] == "2026-03-05T15:30:00.000000Z" and bars[0]["source"] == "v1-archive"
    assert bars[1]["bar_end"] == "2026-03-05T21:00:00.000000Z"                                 # 最后一根不越过收盘（美东 16:00）
    assert fw.execute("SELECT payload_json FROM v1_prediction_archive").fetchone()[0] == '{"l_hat":1}'
    # 幂等：重复导入不重复写
    v, m = va.open_ro(v1), va.open_ro(ml)
    assert va.import_orders(v, lw, account_id="A")["inserted"] == 0 and va.import_hourly(m, mw)["skipped_existing"] == 2
    assert va.import_capital_flow(v, mw) == 0 and va.import_preopen(m, mw) == 0 and va.import_predictions(m, fw) == 0


class OrdersApi:
    def __init__(self, rows):
        self.rows = rows

    def orders(self, acc_id, market, start, end):
        return [r for r in self.rows if str(start) <= r["create_time"][:10] <= str(end)]


def test_futu_orders_update_status_and_names_prefer_futu(dbs):
    lw, _mw, _fw = dbs
    upsert_names(lw, {"US.NVDA": "V1名"}, "v1")
    api = OrdersApi([{"order_id": "O9", "code": "US.NVDA", "stock_name": "英伟达", "side": "BUY", "order_type": "NORMAL", "status": "SUBMITTED", "price": 100.0, "qty": 10,
                      "dealt_qty": 0, "dealt_avg_price": 0, "create_time": "2026-03-05 10:00:00", "updated_time": "2026-03-05 10:00:01"}])
    r1 = collect_orders(lw, api, account_id="A", acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), sleep=lambda s: None, min_interval=0)
    assert r1.inserted == 1 and r1.ok
    api.rows[0].update(status="FILLED_ALL", dealt_qty=10, dealt_avg_price=99.5, updated_time="2026-03-05 10:05:00")
    r2 = collect_orders(lw, api, account_id="A", acc_id=1, markets=["US"], start=date(2026, 3, 1), end=date(2026, 3, 31), sleep=lambda s: None, min_interval=0)
    assert r2.inserted == 1                                                                    # 状态更新
    row = lw.execute("SELECT status, dealt_qty, dealt_avg_price FROM broker_order WHERE order_id='O9'").fetchone()
    assert (row["status"], row["dealt_qty"], row["dealt_avg_price"]) == ("FILLED_ALL", "10", "99.5")
    assert lw.execute("SELECT name, source FROM instrument_name WHERE code='US.NVDA'").fetchone()["name"] == "英伟达"       # 富途名称覆盖 V1
    upsert_names(lw, {"US.NVDA": "又是V1"}, "v1")
    assert lw.execute("SELECT name FROM instrument_name WHERE code='US.NVDA'").fetchone()["name"] == "英伟达"            # V1 不得覆盖富途
