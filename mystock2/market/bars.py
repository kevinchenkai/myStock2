"""日线与小时线的版本化存储（实施方案 WP4.1、4.3）。

- 原始（未复权）价与复权收盘价分列；**权益估值只用原始价**，复权价只用于特征（§6A.5）。
- 行情被修订：内容变化则追加新版本，旧版本保留；内容相同重复写入是 no-op。
- 缺失显式：`missing_sessions` 对照交易日历列出缺口，**不用上一日数据冒充今日，不记零**。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from mystock2.core import calendars as cal
from mystock2.core.db import atomic
from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import ensure_utc, iso_utc, utc_now
from mystock2.instruments.code_map import market_of


class BarError(ValueError):
    pass


@dataclass(frozen=True)
class DailyBar:
    code: str
    session_date: date
    open: str
    high: str
    low: str
    close: str
    adj_close: str | None = None
    volume: str | None = None


@dataclass(frozen=True)
class HourlyBar:
    code: str
    bar_start: object       # datetime（带时区）
    bar_end: object
    open: str
    high: str
    low: str
    close: str
    volume: str | None = None
    complete: bool = True


def _check_ohlc(o, h, lo, c) -> None:
    o, h, lo, c = dec(o), dec(h), dec(lo), dec(c)
    if min(o, h, lo, c) <= 0:
        raise BarError("价格必须为正")
    if not (lo <= min(o, c) and h >= max(o, c) and lo <= h):
        raise BarError(f"OHLC 不自洽：O={o} H={h} L={lo} C={c}")


def _hash(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def put_daily(conn: sqlite3.Connection, bars: list[DailyBar], *, source: str, received_at=None, quality: str = "ok") -> dict[str, int]:
    """幂等写入日线；返回 {'inserted','new_version','duplicate'} 计数。"""
    counts = {"inserted": 0, "new_version": 0, "duplicate": 0}
    received = iso_utc(received_at or utc_now())
    with atomic(conn):
        for b in bars:
            m = market_of(b.code)
            _check_ohlc(b.open, b.high, b.low, b.close)
            if not cal.is_session(m, b.session_date):
                raise BarError(f"{b.code} {b.session_date} 不是 {m} 交易日（拒绝写入非交易日行情）")
            fields = {"code": b.code, "session_date": b.session_date.isoformat(), "source": source,
                      "open": to_db(b.open), "high": to_db(b.high), "low": to_db(b.low), "close": to_db(b.close),
                      "adj_close": to_db(b.adj_close) if b.adj_close is not None else None,
                      "volume": to_db(b.volume) if b.volume is not None else None, "quality": quality}
            h = _hash(fields)
            last = conn.execute("SELECT version, content_hash FROM quote_daily WHERE code=? AND session_date=? ORDER BY version DESC LIMIT 1",
                                (b.code, fields["session_date"])).fetchone()
            if last and last["content_hash"] == h:
                counts["duplicate"] += 1
                continue
            version = (last["version"] + 1) if last else 1
            conn.execute(
                "INSERT INTO quote_daily(code, session_date, version, source, open, high, low, close, adj_close, volume, event_at, received_at, quality, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (b.code, fields["session_date"], version, source, fields["open"], fields["high"], fields["low"], fields["close"], fields["adj_close"],
                 fields["volume"], iso_utc(cal.session(m, b.session_date).close_utc), received, quality, h))
            counts["new_version" if last else "inserted"] += 1
    return counts


def get_daily(conn: sqlite3.Connection, code: str, start: date, end: date, *, source: str | None = None, received_by=None) -> list[sqlite3.Row]:
    """每个交易日取最新版本（可限定来源；received_by 限定「当时已收到」的版本，供点时复算）。"""
    sql = "SELECT * FROM quote_daily WHERE code=? AND session_date BETWEEN ? AND ?"
    args: list = [code, start.isoformat(), end.isoformat()]
    if source:
        sql += " AND source=?"
        args.append(source)
    if received_by is not None:
        sql += " AND received_at<=?"
        args.append(iso_utc(received_by))
    rows = conn.execute(sql + " ORDER BY session_date, version", args).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        cur = latest.get(r["session_date"])
        # 终值（ok）优先于更晚到的 partial；同等质量取最高版本（版本号全局递增）
        if cur is None or r["quality"] == "ok" or cur["quality"] != "ok":
            latest[r["session_date"]] = r
    return [latest[k] for k in sorted(latest)]


def missing_sessions(conn: sqlite3.Connection, code: str, start: date, end: date, *, source: str | None = None) -> list[date]:
    """区间内应有行情而没有的交易日（日历为准；缺失不填充）。"""
    have = {r["session_date"] for r in get_daily(conn, code, start, end, source=source)}
    return [d for d in cal.session_days(market_of(code), start, end) if d.isoformat() not in have]


def put_hourly(conn: sqlite3.Connection, bars: list[HourlyBar], *, source: str, received_at=None) -> dict[str, int]:
    counts = {"inserted": 0, "new_version": 0, "duplicate": 0}
    received = iso_utc(received_at or utc_now())
    with atomic(conn):
        for b in bars:
            m = market_of(b.code)
            _check_ohlc(b.open, b.high, b.low, b.close)
            start, end = ensure_utc(b.bar_start), ensure_utc(b.bar_end)
            if not start < end:
                raise BarError("bar_start 必须早于 bar_end")
            fields = {"code": b.code, "bar_start": iso_utc(start), "bar_end": iso_utc(end), "source": source, "open": to_db(b.open),
                      "high": to_db(b.high), "low": to_db(b.low), "close": to_db(b.close),
                      "volume": to_db(b.volume) if b.volume is not None else None, "complete": int(b.complete)}
            h = _hash(fields)
            last = conn.execute("SELECT version, content_hash FROM quote_hourly WHERE code=? AND bar_start=? ORDER BY version DESC LIMIT 1",
                                (b.code, fields["bar_start"])).fetchone()
            if last and last["content_hash"] == h:
                counts["duplicate"] += 1
                continue
            version = (last["version"] + 1) if last else 1
            conn.execute(
                "INSERT INTO quote_hourly(code, bar_start, version, bar_end, source, open, high, low, close, volume, complete, received_at, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (b.code, fields["bar_start"], version, fields["bar_end"], source, fields["open"], fields["high"], fields["low"], fields["close"],
                 fields["volume"], fields["complete"], received, h))
            counts["new_version" if last else "inserted"] += 1
            del m
    return counts


def hourly_archive_gaps(conn: sqlite3.Connection, code: str, start: date, end: date) -> list[dict]:
    """小时线归档完整性（粗检）：每个交易日至少有一根完整的 bar；bar 落在开收盘之内且不在午休内；不存在则列为缺口。

    只做结构检查，**不**推断每个 bar 应有的精确切分（供应商切分口径由 M0a 核实后再加严）。
    """
    m = market_of(code)
    problems: list[dict] = []
    for d in cal.session_days(m, start, end):
        s = cal.session(m, d)
        rows = conn.execute(
            "SELECT bar_start, bar_end, complete FROM quote_hourly WHERE code=? AND bar_start>=? AND bar_start<? ORDER BY bar_start, version",
            (code, iso_utc(s.open_utc), iso_utc(s.close_utc))).fetchall()
        if not rows:
            problems.append({"session": d.isoformat(), "problem": "no_bars"})
            continue
        for r in rows:
            bs, be = ensure_utc(r["bar_start"]), ensure_utc(r["bar_end"])
            if be > s.close_utc:
                problems.append({"session": d.isoformat(), "problem": "bar_after_close", "bar_start": r["bar_start"]})
            if s.break_start_utc and bs >= s.break_start_utc and bs < s.break_end_utc:
                problems.append({"session": d.isoformat(), "problem": "bar_in_break", "bar_start": r["bar_start"]})
        if not any(r["complete"] for r in rows):
            problems.append({"session": d.isoformat(), "problem": "no_complete_bar"})
    return problems
