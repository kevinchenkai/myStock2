"""行情采集（WP4.1）：逐标的选主/备源，失败/空结果/陈旧保留回执，不记零、不拿昨日冒充今日。

数据源通过 `QuoteSource` 协议注入，单测用假源；`YFinanceSource` 是真实实现：
固定 `auto_adjust=False`（拿到原始价与 Adj Close 分列）、`actions=False`，并把 float64 以 4 位小数入库（供应商价格本身是 float）。
yfinance 是公开接口的个人研究工具，不保证实时（实施方案 §3.2）；小时线可回溯范围有限，故**从首日起归档**（WP4.3）。
"""
from __future__ import annotations

import dataclasses
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
from mystock2.market.bars import BarError, DailyBar, HourlyBar, _check_ohlc, get_daily, put_daily, put_hourly
from mystock2.market.fx import put_rate

FINAL_BUFFER = timedelta(minutes=15)     # 收盘后多久才认为日线为终值（保守；供应商口径由 M0a 核实）


class QuoteSource(Protocol):
    name: str

    def daily(self, code: str, start: date, end: date) -> list[DailyBar]: ...
    def hourly(self, code: str, start: date, end: date) -> list[HourlyBar]: ...
    def fx_daily(self, pair: str, start: date, end: date) -> list[tuple[date, str]]: ...


class VolumeSource(Protocol):
    """日线补源（用于「占位日线」：成交量为 0/缺失的终值日线，如港股半日市 yfinance 给的价是平的、量是 0）。
    返回 {交易日: {"open","high","low","close","volume"}（字符串；只含成交量为正的日子）}。"""
    name: str

    def bars(self, code: str, start: date, end: date) -> dict[date, dict[str, str]]: ...


def _zero_volume(v) -> bool:
    try:
        return v is None or not float(v) > 0
    except (TypeError, ValueError):
        return True


def _is_flat(o, h, lo, c) -> bool:
    try:
        return dec(o) == dec(h) == dec(lo) == dec(c)
    except (MoneyError, ArithmeticError, ValueError, TypeError):
        return False


def _placeholder(o, h, lo, c, v) -> bool:
    """占位日线：成交量为 0/缺失，或价格是平的（开=高=低=收；供应商对半日市等日子的占位）。"""
    return _zero_volume(v) or _is_flat(o, h, lo, c)


def _merge_fallback(o, h, lo, c, adj, vol, fb: dict[str, str]) -> tuple[str, str, str, str, str | None, str, bool]:
    """占位日线：成交量为 0/缺失的取补源的成交量；价格是平的（开=高=低=收）的价格也取补源的（补源 OHLC 自相矛盾则不动价格）。
    复权收盘价按原复权比例换算（不改比例）。返回 (开, 高, 低, 收, 复权收, 量, 是否改了价格)。"""
    vol = vol if not _zero_volume(vol) else fb["volume"]
    if _is_flat(o, h, lo, c):
        try:
            _check_ohlc(fb["open"], fb["high"], fb["low"], fb["close"])
            ratio = dec(adj) / dec(c) if adj is not None and dec(c) > 0 else None
            new_adj = _px(float(dec(fb["close"]) * ratio)) if ratio is not None else None
            return fb["open"], fb["high"], fb["low"], fb["close"], new_adj, vol, True
        except (BarError, MoneyError, ArithmeticError, ValueError, KeyError):
            pass
    return o, h, lo, c, adj, vol, False


def fill_zero_volume(bars: list[DailyBar], src: VolumeSource, code: str) -> tuple[list[DailyBar], list[str], str | None, list[str]]:
    """把成交量为 0/缺失的终值日线用补源补上（见 _merge_fallback）；补不到的保持原样（下游把 0 当缺失跳过，不编造）。
    返回 (新 bar 列表, 已补成交量的日期, 补源错误, 同时改了价格的日期)。补源任何异常都不影响价格入库。"""
    need = [b for b in bars if _placeholder(b.open, b.high, b.low, b.close, b.volume)]
    if not need:
        return bars, [], None, []
    try:
        fb = src.bars(code, min(b.session_date for b in need), max(b.session_date for b in need))
    except Exception as exc:  # noqa: BLE001 —— OpenD 未开/限频等：补不上不拖垮行情采集
        return bars, [], f"{type(exc).__name__}: {exc}", []
    out, filled, priced = [], [], []
    for b in bars:
        f = fb.get(b.session_date) if _placeholder(b.open, b.high, b.low, b.close, b.volume) else None
        if f is None:
            out.append(b)
            continue
        o, h, lo, c, adj, vol, changed = _merge_fallback(b.open, b.high, b.low, b.close, b.adj_close, b.volume, f)
        out.append(dataclasses.replace(b, open=o, high=h, low=lo, close=c, adj_close=adj, volume=vol))
        filled.append(b.session_date.isoformat())
        if changed:
            priced.append(b.session_date.isoformat())
    return out, filled, None, priced


def repair_zero_volume(conn: sqlite3.Connection, src: VolumeSource, codes: list[str], start: date, end: date, *,
                       run_id: str | None = None, now: datetime | None = None) -> dict:
    """历史修补：库里「最新版本是占位日线（成交量为 0/缺失，或价格是平的）」的终值日线，用补源追加一个新版本（成交量；价格是平的则价格也取补源；
    来源标记不变，只追加，不改旧行）。只在确有此类行时才连补源；补不到的日子原样保留并列出。"""
    now = now or utc_now()
    out: dict = {}
    for code in codes:
        rows = [r for r in get_daily(conn, code, start, end) if r["quality"] == "ok" and _placeholder(r["open"], r["high"], r["low"], r["close"], r["volume"])]
        if not rows:
            out[code] = {"status": "ok", "need": 0, "repaired": 0, "priced": [], "unavailable": []}
            continue
        try:
            fb = src.bars(code, date.fromisoformat(rows[0]["session_date"]), date.fromisoformat(rows[-1]["session_date"]))
        except Exception as exc:  # noqa: BLE001
            with atomic(conn):
                _log(conn, run_id, code, "volume_repair", src.name, "error", detail=f"{type(exc).__name__}: {exc}", at=now)
            out[code] = {"status": "failed", "need": len(rows), "repaired": 0, "error": f"{type(exc).__name__}: {exc}"}
            continue
        done, missing, priced = 0, [], []
        with atomic(conn):
            for r in rows:
                d = date.fromisoformat(r["session_date"])
                f = fb.get(d)
                if f is None:
                    missing.append(r["session_date"])
                    continue
                o, h, lo, c, adj, vol, changed = _merge_fallback(r["open"], r["high"], r["low"], r["close"], r["adj_close"], r["volume"], f)
                put_daily(conn, [DailyBar(code, d, o, h, lo, c, adj, vol)], source=r["source"], received_at=now, quality="ok")
                done += 1
                if changed:
                    priced.append(r["session_date"])
            _log(conn, run_id, code, "volume_repair", src.name, "ok" if not missing else "partial", rows=done,
                 detail=f"need={len(rows)} price_replaced={priced} unavailable={missing}", at=now)
        out[code] = {"status": "ok" if not missing else "partial", "need": len(rows), "repaired": done, "priced": priced, "unavailable": missing}
    return out


def _log(conn, run_id, code, kind, source, status, rows=0, detail=None, at=None) -> None:
    attempt = hashlib.sha256(f"{run_id}|{code}|{kind}|{source}|{iso_utc(at or utc_now())}|{status}".encode()).hexdigest()[:24]
    conn.execute("INSERT OR IGNORE INTO collection_log(attempt_id, run_id, code, kind, source, status, rows, detail, attempted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (attempt, run_id, code, kind, source, status, rows, detail, iso_utc(at or utc_now())))


def collect_daily(conn: sqlite3.Connection, sources: list[QuoteSource], code: str, start: date, end: date, *,
                  run_id: str | None = None, now: datetime | None = None, volume_source: VolumeSource | None = None) -> dict:
    """按顺序尝试各源，第一个返回非空数据的源获胜。返回 {'status','source','rows','attempts'}。
    给了 volume_source 时，终值日线里成交量为 0/缺失的（如港股半日市，yfinance 给 0）用它补成交量；补不到的原样入库。"""
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
        vol_note = ""
        if volume_source is not None and final:
            final, filled, vol_err, priced = fill_zero_volume(final, volume_source, code)
            if filled or vol_err:
                vol_note = (f" volume_filled_by_{volume_source.name}={filled} price_replaced_by_{volume_source.name}={priced}"
                            + (f" volume_source_error={vol_err}" if vol_err else ""))
        with atomic(conn):
            counts = put_daily(conn, final, source=src.name, received_at=now, quality="ok") if final else {}
            if partial:
                put_daily(conn, partial, source=src.name, received_at=now, quality="partial")
            suspect = counts.pop("suspect_dates", []) if counts else []
            warn = f" 疑似供应商回溯拆股调整（新旧收盘价成整数倍，需人工核对）：{suspect}" if suspect else ""
            _log(conn, run_id, code, "daily", src.name, "partial" if (partial or invalid or suspect) else "ok", rows=len(keep),
                 detail=f"final={len(final)} partial={len(partial)} dropped_non_session={dropped} rejected_invalid_ohlc={invalid} {counts}{warn}{vol_note}", at=now)
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


# ---------------------------------------------------------------- 富途成交量补源
class FutuVolumeSource:
    """富途历史日 K 线补源（只读行情查询，不涉及交易与账户；需本机 OpenD 在线并有该市场行情权限）。
    原始价口径（不复权）；成交量单位与 yfinance 一致（港股为股数）；成交量为 0（停牌/无数据）的日子不返回。"""
    name = "futu"

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self.host, self.port = host, port

    def bars(self, code: str, start: date, end: date) -> dict[date, dict[str, str]]:
        from futu import RET_OK, AuType, KLType, OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        out: dict[date, dict[str, str]] = {}
        try:
            key = None
            while True:
                ret, df, key = ctx.request_history_kline(code, start=start.isoformat(), end=end.isoformat(), ktype=KLType.K_DAY,
                                                         autype=AuType.NONE, max_count=1000, page_req_key=key)
                if ret != RET_OK:
                    raise RuntimeError(str(df))
                for _, r in df.iterrows():
                    v = float(r["volume"])
                    if math.isfinite(v) and v > 0:
                        out[date.fromisoformat(str(r["time_key"])[:10])] = {"open": _px(r["open"]), "high": _px(r["high"]), "low": _px(r["low"]),
                                                                           "close": _px(r["close"]), "volume": str(int(v))}
                if key is None:
                    return out
        finally:
            ctx.close()
