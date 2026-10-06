"""AI 评价缓存的只读访问（Web 与 CLI 共用；不写库、不调用模型）。

同一笔成交可以有多行（每次请求或刷新新增一行，不覆盖旧评价）；页面取最新一行。
状态：running（后台进程在跑）→ ok / error。running 超时或进程已不在 → 视为中断（error）。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from mystock2.core.timeutil import ensure_utc

DEFAULT_TIMEOUT_S = 900
GRACE_S = 60


def latest(conn: sqlite3.Connection, deal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM trade_review WHERE deal_id=? ORDER BY review_id DESC LIMIT 1", (deal_id,)).fetchone()


def latest_ok(conn: sqlite3.Connection, deal_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM trade_review WHERE deal_id=? AND status='ok' ORDER BY review_id DESC LIMIT 1", (deal_id,)).fetchone()


def history_count(conn: sqlite3.Connection, deal_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM trade_review WHERE deal_id=? AND status='ok'", (deal_id,)).fetchone()[0]


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running(row: sqlite3.Row | None, now: datetime, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    """running 行只有在进程还活着、且没超过超时（含宽限）时才算真的在跑。"""
    if row is None or row["status"] != "running":
        return False
    age = (ensure_utc(now) - ensure_utc(row["requested_at"])).total_seconds()
    return age <= timeout_s + GRACE_S and _pid_alive(row["pid"])
