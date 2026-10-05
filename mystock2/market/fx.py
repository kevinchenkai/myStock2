"""汇率（USD/HKD/CNY 换算路径）。缺汇率则「不可用」，不静默当作 1，也不拿陈旧值冒充当日。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal

from mystock2.core.db import atomic
from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import iso_utc, utc_now


class FxUnavailable(LookupError):
    pass


def put_rate(conn: sqlite3.Connection, pair: str, rate_date: date, rate: str, *, source: str, event_at, received_at=None) -> str:
    pair = pair.upper()
    if len(pair) != 6 or not pair.isalpha() or pair[:3] == pair[3:]:
        raise ValueError(f"币对格式应为 USDHKD：{pair!r}")
    r = dec(rate)
    if r <= 0:
        raise ValueError("汇率必须为正")
    fields = {"pair": pair, "rate_date": rate_date.isoformat(), "source": source, "rate": to_db(r)}
    h = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    with atomic(conn):
        last = conn.execute("SELECT version, content_hash FROM fx_rate WHERE pair=? AND rate_date=? ORDER BY version DESC LIMIT 1",
                            (pair, fields["rate_date"])).fetchone()
        if last and last["content_hash"] == h:
            return "duplicate"
        conn.execute("INSERT INTO fx_rate(pair, rate_date, version, source, rate, event_at, received_at, content_hash) VALUES (?,?,?,?,?,?,?,?)",
                     (pair, fields["rate_date"], (last["version"] + 1) if last else 1, source, fields["rate"], iso_utc(event_at), iso_utc(received_at or utc_now()), h))
    return "new_version" if last else "inserted"


def get_rate(conn: sqlite3.Connection, pair: str, on: date, *, max_stale_days: int = 0) -> tuple[Decimal, dict]:
    """取 on 当日（或最多回退 max_stale_days 天，默认 0＝必须当日）的汇率；返回 (rate, 来源信息)。直接币对或其反向。"""
    pair = pair.upper()
    for p, inverse in ((pair, False), (pair[3:] + pair[:3], True)):
        row = conn.execute(
            "SELECT * FROM fx_rate WHERE pair=? AND rate_date<=? AND rate_date>=date(?, ?) ORDER BY rate_date DESC, version DESC LIMIT 1",
            (p, on.isoformat(), on.isoformat(), f"-{max_stale_days} day")).fetchone()
        if row:
            rate = dec(row["rate"])
            info = {"pair": p, "rate_date": row["rate_date"], "source": row["source"], "received_at": row["received_at"],
                    "inverse": inverse, "stale_days": (on - date.fromisoformat(row["rate_date"])).days}
            return (Decimal(1) / rate if inverse else rate), info
    raise FxUnavailable(f"汇率不可用：{pair} {on}（max_stale_days={max_stale_days}）")


def convert(conn: sqlite3.Connection, amount: str, from_ccy: str, to_ccy: str, on: date, *, max_stale_days: int = 0) -> tuple[Decimal, dict]:
    """显式换算并返回汇率来源；同币种直接返回。仅用于展示，不进入账本。"""
    if from_ccy.upper() == to_ccy.upper():
        return dec(amount), {"pair": None}
    rate, info = get_rate(conn, from_ccy.upper() + to_ccy.upper(), on, max_stale_days=max_stale_days)
    return dec(amount) * rate, info
