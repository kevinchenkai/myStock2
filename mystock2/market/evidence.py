"""不可变证据快照（实施方案 WP4.2、F07、T-24、T-38）。

- 每次行情/资料被预测或操作单引用，都先存成快照（内容哈希）；修订与回填只能追加新快照。
- `time_trust`：`exact`（实际接收时间已知）或 `assumed_bar_end`（历史研究按 bar 结束推定可得，不补造真实接收时间）。
- `verify_inputs` 做时间链校验：每个输入的 `received_at` 必须不晚于该版本的输入截止。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from mystock2.core.db import atomic
from mystock2.core.timeutil import ensure_utc, iso_utc, utc_now
from mystock2.market.bars import get_daily


class EvidenceError(ValueError):
    pass


def snapshot(conn: sqlite3.Connection, kind: str, subject: str, content: dict, *, event_at=None, available_at=None,
             received_at=None, time_trust: str = "exact") -> str:
    """幂等写入证据快照，返回 snapshot_id。"""
    if time_trust not in ("exact", "assumed_bar_end"):
        raise EvidenceError("time_trust 非法")
    body = json.dumps(content, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(f"{kind}|{subject}|{body}".encode("utf-8")).hexdigest()
    sid = h[:24]
    with atomic(conn):
        if conn.execute("SELECT 1 FROM evidence_snapshot WHERE snapshot_id=?", (sid,)).fetchone():
            return sid
        conn.execute(
            "INSERT INTO evidence_snapshot(snapshot_id, kind, subject, content_json, content_hash, event_at, available_at, received_at, time_trust, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, kind, subject, body, h, iso_utc(event_at) if event_at else None, iso_utc(available_at) if available_at else None,
             iso_utc(received_at or utc_now()), time_trust, iso_utc(utc_now())))
    return sid


def snapshot_daily_bars(conn: sqlite3.Connection, code: str, start: date, end: date, *, received_by=None, time_trust: str = "exact") -> str:
    """把 [start, end] 的日线（每日最新版本，可限定「当时已收到」）冻结为一个快照；快照内含每根 bar 的版本与内容哈希。"""
    rows = get_daily(conn, code, start, end, received_by=received_by)
    if not rows:
        raise EvidenceError(f"{code} {start}~{end} 没有行情，无法生成证据快照")
    content = {"code": code, "bars": [
        {k: r[k] for k in ("session_date", "version", "source", "open", "high", "low", "close", "adj_close", "volume", "event_at", "received_at", "quality", "content_hash")}
        for r in rows]}
    last_event = max(r["event_at"] for r in rows)
    last_received = max(r["received_at"] for r in rows)
    return snapshot(conn, "daily_bars", code, content, event_at=last_event, available_at=last_event, received_at=last_received, time_trust=time_trust)


def verify_inputs(conn: sqlite3.Connection, snapshot_ids: list[str], input_cutoff_at) -> list[str]:
    """返回违反时间链的原因列表（空＝合规）：任一输入的 received_at 晚于输入截止，或快照不存在。"""
    cutoff = ensure_utc(input_cutoff_at)
    problems = []
    for sid in snapshot_ids:
        row = conn.execute("SELECT received_at, time_trust FROM evidence_snapshot WHERE snapshot_id=?", (sid,)).fetchone()
        if not row:
            problems.append(f"missing_snapshot:{sid}")
            continue
        if ensure_utc(row["received_at"]) > cutoff:
            problems.append(f"received_after_input_cutoff:{sid}")
    return problems
