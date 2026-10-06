"""行情采集（WP4.1）：逐标的选主/备源，失败/空结果/陈旧保留回执，不记零、不拿昨日冒充今日。

数据源通过 `QuoteSource` 协议注入，单测用假源；`YFinanceSource` 是真实实现：
固定 `auto_adjust=False`（拿到原始价与 Adj Close 分列）、`actions=False`，并把 float64 以 4 位小数入库（供应商价格本身是 float）。
yfinance 是公开接口的个人研究工具，不保证实时（实施方案 §3.2）；小时线可回溯范围有限，故**从首日起归档**（WP4.3）。
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Protocol

from mystock2.core import calendars as cal
from mystock2.core.db import atomic
from mystock2.core.money import MoneyError, dec
from mystock2.core.timeutil import MARKET_TZ, iso_utc, utc_now
from mystock2.instruments.code_map import futu_to_yf, market_of
from mystock2.market.bars import BarError, DailyBar, HourlyBar, _check_ohlc, put_daily, put_hourly
from mystock2.market.fx import put_rate

FINAL_BUFFER = timedelta(minutes=15)     # 收盘后多久才认为日线为终值（保守；供应商口径由 M0a 核实）


class QuoteSource(Protocol):
    name: str

    def daily(self, code: str, start: date, end: date) -> list[DailyBar]: ...
    def hourly(self, code: str, start: date, end: date) -> list[HourlyBar]: ...
    def fx_daily(self, pair: str, start: date, end: date) -> list[tuple[date, str]]: ...


def _log(conn, run_id, code, kind, source, status, rows=0, detail=None, at=None) -> None:
    attempt = hashlib.sha256(f"{run_id}|{code}|{kind}|{source}|{iso_utc(at or utc_now())}|{status}".encode()).hexdigest()[:24]
    conn.execute("INSERT OR IGNORE INTO collection_log(attempt_id, run_id, code, kind, source, status, rows, detail, attempted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (attempt, run_id, code, kind, source, status, rows, detail, iso_utc(at or utc_now())))


def collect_daily(conn: sqlite3.Connection, sources: list[QuoteSource], code: str, start: date, end: date, *,
                  run_id: str | None = None, now: datetime | None = None) -> dict:
    """按顺序尝试各源，第一个返回非空数据的源获胜。返回 {'status','source','rows','attempts'}。"""
    now = now or utc_now()
    market = market_of(code)
    attempts = []
    for src in sources:
        try:
            bars = src.daily(code, start, end)
        except Exception as exc:  # noqa: BLE001 —— 任何供应商异常都只是这一源失败
            with atomic(conn):
                _log(conn, run_id, code, "daily", src.name, "error", detail=f"{type(exc).__name__}: {exc}", at=now)
            attempts.append((src.name, "error"))
            continue
        # 只保留日历内的交易日（供应商可能返回非交易日或未走完的当日 bar）
        keep, dropped, invalid = [], 0, []
        for b in bars:
            if not cal.is_session(market, b.session_date):
                dropped += 1
                continue
            try:                                              # 供应商偶有 OHLC 自相矛盾的行：只丢这一行（留作缺口，不修补不记零），不拖垮整个标的
                _check_ohlc(b.open, b.high, b.low, b.close)
            except (BarError, MoneyError):                  # 含 NaN/非数值（退市或停牌标的常见）
                invalid.append(b.session_date.isoformat())
                continue
            keep.append(b)
        if not keep:
            with atomic(conn):
                _log(conn, run_id, code, "daily", src.name, "empty", detail=f"dropped_non_session={dropped}", at=now)
            attempts.append((src.name, "empty"))
            continue
        final, partial = [], []
        for b in keep:
            (final if cal.session(market, b.session_date).close_utc + FINAL_BUFFER <= now else partial).append(b)
        with atomic(conn):
            counts = put_daily(conn, final, source=src.name, received_at=now, quality="ok") if final else {}
            if partial:
                put_daily(conn, partial, source=src.name, received_at=now, quality="partial")
            _log(conn, run_id, code, "daily", src.name, "partial" if (partial or invalid) else "ok", rows=len(keep),
                 detail=f"final={len(final)} partial={len(partial)} dropped_non_session={dropped} rejected_invalid_ohlc={invalid} {counts}", at=now)
        attempts.append((src.name, "ok"))
        return {"status": "partial" if (partial or invalid) else "ok", "rejected_invalid_ohlc": invalid, "source": src.name, "rows": len(keep), "attempts": attempts}
    return {"status": "failed", "source": None, "rows": 0, "attempts": attempts}


def collect_hourly(conn, sources: list[QuoteSource], code: str, start: date, end: date, *, run_id: str | None = None, now: datetime | None = None) -> dict:
    now = now or utc_now()
    attempts = []
    for src in sources:
        try:
            bars = src.hourly(code, start, end)
        except Exception as exc:  # noqa: BLE001
            with atomic(conn):
                _log(conn, run_id, code, "hourly", src.name, "error", detail=f"{type(exc).__name__}: {exc}", at=now)
            attempts.append((src.name, "error"))
            continue
        if not bars:
            with atomic(conn):
                _log(conn, run_id, code, "hourly", src.name, "empty", at=now)
            attempts.append((src.name, "empty"))
            continue
        good = []
        for b in bars:
            try:
                _check_ohlc(b.open, b.high, b.low, b.close)
                good.append(b)
            except (BarError, MoneyError):
                pass                                          # 同日线：坏行不入库（留作缺口）
        bars = good
        if not bars:
            with atomic(conn):
                _log(conn, run_id, code, "hourly", src.name, "empty", detail="all rows invalid", at=now)
            attempts.append((src.name, "empty"))
            continue
        with atomic(conn):
            counts = put_hourly(conn, bars, source=src.name, received_at=now)
            _log(conn, run_id, code, "hourly", src.name, "ok", rows=len(bars), detail=str(counts), at=now)
        attempts.append((src.name, "ok"))
        return {"status": "ok", "source": src.name, "rows": len(bars), "attempts": attempts}
    return {"status": "failed", "source": None, "rows": 0, "attempts": attempts}


def collect_fx(conn, sources: list[QuoteSource], pair: str, start: date, end: date, *, run_id: str | None = None, now: datetime | None = None) -> dict:
    now = now or utc_now()
    attempts = []
    for src in sources:
        try:
            rates = src.fx_daily(pair, start, end)
        except Exception as exc:  # noqa: BLE001
            with atomic(conn):
                _log(conn, run_id, pair, "fx", src.name, "error", detail=f"{type(exc).__name__}: {exc}", at=now)
            attempts.append((src.name, "error"))
            continue
        if not rates:
            with atomic(conn):
                _log(conn, run_id, pair, "fx", src.name, "empty", at=now)
            attempts.append((src.name, "empty"))
            continue
        good, bad = [], 0
        for d, rate in rates:                                # 逐行校验：一行 NaN/非正不拖垮整个币对（审核 C-05），坏行留作缺口
            try:
                if dec(str(rate)) <= 0:
                    raise MoneyError("非正汇率")
                good.append((d, rate))
            except (MoneyError, ArithmeticError, ValueError):
                bad += 1
        if not good:
            with atomic(conn):
                _log(conn, run_id, pair, "fx", src.name, "empty", detail="all rows invalid", at=now)
            attempts.append((src.name, "empty"))
            continue
        with atomic(conn):
            for d, rate in good:
                put_rate(conn, pair, d, rate, source=src.name, event_at=datetime(d.year, d.month, d.day, 23, 59, tzinfo=utc_now().tzinfo), received_at=now)
            _log(conn, run_id, pair, "fx", src.name, "ok", rows=len(good), detail=f"dropped_invalid={bad}" if bad else None, at=now)
        attempts.append((src.name, "ok"))
        return {"status": "ok", "source": src.name, "rows": len(good), "attempts": attempts}
    return {"status": "failed", "source": None, "rows": 0, "attempts": attempts}


# ---------------------------------------------------------------- yfinance 真实源
def _px(x) -> str:
    return f"{float(x):.4f}"


def _vol(x) -> str | None:
    """成交量：NaN / inf → 缺失（不让一行脏数据让整只标的失败，审核 P3）。"""
    v = float(x)
    return str(int(v)) if math.isfinite(v) else None


class YFinanceSource:
    name = "yfinance"

    def _history(self, symbol: str, start: date, end: date, interval: str):
        import yfinance as yf  # 延迟导入：核心与测试不依赖网络库
        return yf.Ticker(symbol).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), interval=interval,
                                         auto_adjust=False, actions=False, raise_errors=True)

    def daily(self, code: str, start: date, end: date) -> list[DailyBar]:
        df = self._history(futu_to_yf(code), start, end, "1d")
        out = []
        for idx, r in df.iterrows():
            out.append(DailyBar(code, idx.date(), _px(r["Open"]), _px(r["High"]), _px(r["Low"]), _px(r["Close"]),
                                _px(r["Adj Close"]) if "Adj Close" in r and r["Adj Close"] == r["Adj Close"] else None,
                                _vol(r["Volume"])))
        return out

    def hourly(self, code: str, start: date, end: date) -> list[HourlyBar]:
        df = self._history(futu_to_yf(code), start, end, "1h")
        market, now, out = market_of(code), utc_now(), []
        for idx, r in df.iterrows():
            bs = idx.to_pydatetime()
            be = bs + timedelta(hours=1)
            local_day = bs.astimezone(MARKET_TZ[market]).date()
            if cal.is_session(market, local_day):                      # bar 不得越过收盘（如美股最后一根 30 分钟）
                be = min(be, cal.session(market, local_day).close_utc)
            if be <= bs:
                continue
            out.append(HourlyBar(code, bs, be, _px(r["Open"]), _px(r["High"]), _px(r["Low"]), _px(r["Close"]),
                                 _vol(r["Volume"]), be <= now))
        return out

    def fx_daily(self, pair: str, start: date, end: date) -> list[tuple[date, str]]:
        df = self._history(pair.upper() + "=X", start, end, "1d")
        return [(idx.date(), f"{float(r['Close']):.6f}") for idx, r in df.iterrows()]
