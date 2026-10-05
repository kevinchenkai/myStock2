"""V1 其余 API 数据的一次性、只读迁移（无法从 yfinance/富途回补，或展示需要的参考数据）。

- 订单（含已撤/失败，意图记录）→ `broker_order`；时间是市场本地时间无时区标记：按市场补时区并标 `assumed_local_tz`。
- 标的名称（中文名）→ `instrument_name`；标的档案（行业/市值/估值/52 周）→ `instrument_profile`（描述性参考，不是账本数据）。
- ML 库的 yfinance 小时线（V1 自 2023-06 起归档；yfinance 只给近 60 天，**回补不了**）→ `quote_hourly`（source='v1-archive'；V2 已有的 bar 不重复写）。
- 盘前价（带 available_at）→ `quote_preopen`；资金流向 → `capital_flow_daily`；V1 前向预测版本 → `v1_prediction_archive`（原样 JSON，不与 V2 预测混算）。
V1 库只以 `mode=ro` 打开；V1 的 float 价格按 4 位小数量化入库。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from mystock2.collectors.v1_import import PRICE_Q, V1Error, _local_to_utc, _q
from mystock2.core import calendars as cal
from mystock2.core.db import atomic
from mystock2.core.money import to_db
from mystock2.core.timeutil import MARKET_TZ, iso_utc, utc_now
from mystock2.instruments.code_map import CodeError, market_of
from mystock2.market.bars import BarError, HourlyBar, _check_ohlc, put_hourly

UTC = timezone.utc


def open_ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise V1Error(f"V1 数据库不存在：{p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _num(x) -> str | None:
    return None if x is None else to_db(Decimal(repr(float(x))).quantize(Decimal("0.000001")).normalize())


def _txt(x) -> str | None:
    return None if x is None else str(x)


def _utc_from_naive(text: str) -> str:
    """无时区的 UTC 文本（V1 的 ts_utc / synced_at）→ 规范 UTC。"""
    t = text.strip().replace("T", " ")
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in t else "%Y-%m-%d %H:%M:%S"
    return iso_utc(datetime.strptime(t, fmt).replace(tzinfo=UTC))


def import_names(v1: sqlite3.Connection, ledger: sqlite3.Connection) -> int:
    """名称：持仓表最新一天 > 成交 > 订单（同一代码取最后出现的非空名称）。"""
    names: dict[str, str] = {}
    for table, order in (("orders", "create_time"), ("deals", "create_time"), ("positions", "snapshot_date")):
        if not _has(v1, table):
            continue
        for r in v1.execute(f"SELECT code, name FROM {table} WHERE name IS NOT NULL AND name!='' ORDER BY {order}"):
            names[r["code"]] = r["name"]                                  # 越晚越优先；表的顺序决定同日来源优先级
    n = 0
    now = iso_utc(utc_now())
    with atomic(ledger):
        for code, name in sorted(names.items()):
            try:
                market_of(code)
            except CodeError:
                continue
            cur = ledger.execute("SELECT source FROM instrument_name WHERE code=?", (code,)).fetchone()
            if cur and cur["source"] == "futu":                           # 富途直采的名称优先于 V1
                continue
            ledger.execute("INSERT INTO instrument_name(code, name, source, updated_at) VALUES (?,?,?,?) "
                           "ON CONFLICT(code) DO UPDATE SET name=excluded.name, source=excluded.source, updated_at=excluded.updated_at", (code, name, "v1", now))
            n += 1
    return n


def import_orders(v1: sqlite3.Connection, ledger: sqlite3.Connection, *, account_id: str) -> dict:
    rep = {"read": 0, "inserted": 0, "updated": 0, "skipped": []}
    now = iso_utc(utc_now())
    with atomic(ledger):
        for r in v1.execute("SELECT * FROM orders ORDER BY create_time, order_id"):
            rep["read"] += 1
            try:
                market = market_of(r["code"])
                created = _local_to_utc(market, r["create_time"])
                updated = _local_to_utc(market, r["updated_time"]) if r["updated_time"] else None
            except (CodeError, TypeError, ValueError, KeyError):
                rep["skipped"].append(str(r["order_id"]))
                continue
            side = str(r["trd_side"] or "").upper()
            if side not in ("BUY", "SELL") or not r["order_id"]:
                rep["skipped"].append(str(r["order_id"]))
                continue
            vals = (account_id, str(r["order_id"]), market, r["code"], side, _txt(r["order_type"]), str(r["order_status"] or "UNKNOWN"),
                    _num(r["price"]), _num(r["qty"]), _num(r["dealt_qty"]), _num(r["dealt_avg_price"]), created, updated, "assumed_local_tz", "v1", now)
            cur = ledger.execute("SELECT status, updated_at FROM broker_order WHERE account_id=? AND order_id=?", (account_id, str(r["order_id"]))).fetchone()
            if cur is None:
                ledger.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, dealt_qty, dealt_avg_price, created_at, "
                               "updated_at, time_trust, source, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
                rep["inserted"] += 1
            elif (cur["status"], cur["updated_at"]) != (vals[6], vals[12]) and cur["updated_at"] is not None and (vals[12] or "") > cur["updated_at"]:
                ledger.execute("UPDATE broker_order SET status=?, dealt_qty=?, dealt_avg_price=?, updated_at=? WHERE account_id=? AND order_id=?",
                               (vals[6], vals[9], vals[10], vals[12], account_id, str(r["order_id"])))
                rep["updated"] += 1
    return rep


_PROFILE_COLS = ("long_name", "sector", "industry", "exchange", "currency", "market_cap_mm", "shares_mm", "trailing_pe", "forward_pe", "price_to_book",
                 "trailing_eps", "dividend_yield", "beta", "week52_high", "week52_low", "lot_size", "website")


def import_profiles(v1: sqlite3.Connection, market: sqlite3.Connection) -> int:
    if not _has(v1, "stock_profiles"):
        return 0
    n, now = 0, iso_utc(utc_now())
    with atomic(market):
        for r in v1.execute("SELECT * FROM stock_profiles"):
            code = r["futu_code"]
            try:
                market_of(code)
            except CodeError:
                continue
            vals = [_txt(r[c]) if c in r.keys() else None for c in _PROFILE_COLS]
            market.execute(f"INSERT INTO instrument_profile(code, {', '.join(_PROFILE_COLS)}, source, as_of, updated_at) VALUES (?{',?' * len(_PROFILE_COLS)},?,?,?) "
                           "ON CONFLICT(code) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in _PROFILE_COLS) + ", source=excluded.source, as_of=excluded.as_of, updated_at=excluded.updated_at",
                           (code, *vals, "v1", _txt(r["snap_synced_at"] if "snap_synced_at" in r.keys() else r["synced_at"]), now))
            n += 1
    return n


def import_capital_flow(v1: sqlite3.Connection, market: sqlite3.Connection) -> int:
    if not _has(v1, "capital_flow"):
        return 0
    n = 0
    with atomic(market):
        for r in v1.execute("SELECT * FROM capital_flow ORDER BY code, date"):
            try:
                market_of(r["code"])
            except CodeError:
                continue
            cur = market.execute("INSERT OR IGNORE INTO capital_flow_daily(code, session_date, in_flow, main_in_flow, super_in_flow, big_in_flow, mid_in_flow, sml_in_flow, "
                                 "source, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (r["code"], r["date"], _num(r["in_flow"]), _num(r["main_in_flow"]), _num(r["super_in_flow"]), _num(r["big_in_flow"]),
                                  _num(r["mid_in_flow"]), _num(r["sml_in_flow"]), "v1", _utc_from_naive(r["synced_at"]) if r["synced_at"] and " " in str(r["synced_at"]) else iso_utc(utc_now())))
            n += cur.rowcount
    return n


def import_preopen(ml: sqlite3.Connection, market: sqlite3.Connection) -> int:
    if not _has(ml, "ml_preopen_quotes"):
        return 0
    n = 0
    with atomic(market):
        for r in ml.execute("SELECT * FROM ml_preopen_quotes ORDER BY code, date"):
            try:
                market_of(r["code"])
            except CodeError:
                continue
            avail = r["available_at"]
            cur = market.execute("INSERT OR IGNORE INTO quote_preopen(code, session_date, price, prev_close, available_at, source, received_at) VALUES (?,?,?,?,?,?,?)",
                                 (r["code"], r["date"], _num(r["price"]), _num(r["prev_close"]), _utc_from_naive(avail) if avail else None, "v1:" + str(r["source"]),
                                  _utc_from_naive(r["synced_at"]) if r["synced_at"] else iso_utc(utc_now())))
            n += cur.rowcount
    return n


def import_hourly(ml: sqlite3.Connection, market: sqlite3.Connection) -> dict:
    """V1 ML 库 yfinance 小时线 → quote_hourly（V2 已有同一 bar_start 的行不重复写）。"""
    rep = {"read": 0, "inserted": 0, "skipped_existing": 0, "skipped_invalid": 0}
    if not _has(ml, "ml_quotes_1h"):
        return rep
    have = {(r[0], r[1]) for r in market.execute("SELECT code, bar_start FROM quote_hourly")}
    by_code: dict[str, list[HourlyBar]] = {}
    for r in ml.execute("SELECT * FROM ml_quotes_1h ORDER BY futu_code, ts_utc"):
        rep["read"] += 1
        code = r["futu_code"]
        try:
            m = market_of(code)
            bs = datetime.strptime(r["ts_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            if (code, iso_utc(bs)) in have:
                rep["skipped_existing"] += 1
                continue
            be = bs + timedelta(hours=1)
            local_day = bs.astimezone(MARKET_TZ[m]).date()
            if cal.is_session(m, local_day):
                be = min(be, cal.session(m, local_day).close_utc)
            if be <= bs:
                rep["skipped_invalid"] += 1
                continue
            o, h, lo, c = (str(_q(r[k], PRICE_Q)[0]) for k in ("open", "high", "low", "close"))
            by_code.setdefault(code, []).append(HourlyBar(code, bs, be, o, h, lo, c, str(int(r["volume"])) if r["volume"] is not None else None, True))
        except (CodeError, ValueError, TypeError, KeyError):
            rep["skipped_invalid"] += 1
    for bars in by_code.values():
        good = []
        for b in bars:
            try:
                _check_ohlc(b.open, b.high, b.low, b.close)
                good.append(b)
            except (BarError, ValueError):
                rep["skipped_invalid"] += 1
        counts = put_hourly(market, good, source="v1-archive", received_at=utc_now()) if good else {}
        rep["inserted"] += counts.get("inserted", 0)
    return rep


def import_predictions(ml: sqlite3.Connection, forecast: sqlite3.Connection) -> int:
    if not _has(ml, "ml_prediction_versions"):
        return 0
    n, now = 0, iso_utc(utc_now())
    with atomic(forecast):
        for r in ml.execute("SELECT * FROM ml_prediction_versions ORDER BY as_of, code, prediction_id"):
            try:
                market_of(r["code"])
            except CodeError:
                continue
            cur = forecast.execute(
                "INSERT OR IGNORE INTO v1_prediction_archive(prediction_id, code, as_of, target_session, source, status, generated_at, published_at, payload_json, imported_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(r["prediction_id"]), r["code"], r["as_of"], r["target_session"], r["source"], r["status"], _txt(r["generated_at"]), _txt(r["published_at"]),
                 r["payload_json"], now))
            n += cur.rowcount
    return n
