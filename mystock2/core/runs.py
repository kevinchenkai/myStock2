"""运行回执（run_log）：每次命令输出 run_id、输入引用、成败与可重试范围（实施方案 §9、NF-03）。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from mystock2.core.timeutil import iso_utc, utc_now


def new_run_id() -> str:
    return "r_" + utc_now().strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(3)


class Run:
    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn, self.run_id = conn, run_id
        self.status = "ok"
        self.detail: dict[str, Any] = {}
        self.retry_scope: str | None = None

    def partial(self, retry_scope: str, **detail: Any) -> None:
        self.status, self.retry_scope = "partial", retry_scope
        self.detail.update(detail)

    def note(self, **detail: Any) -> None:
        self.detail.update(detail)


@contextmanager
def run_log(conn: sqlite3.Connection, command: str, inputs: dict[str, Any] | None = None) -> Iterator[Run]:
    """conn 须是 owner='core' 的写连接。异常时记 failed 并继续向上抛出。"""
    run = Run(conn, new_run_id())
    conn.execute(
        "INSERT INTO run_log(run_id, command, inputs_json, started_at, status) VALUES (?,?,?,?, 'running')",
        (run.run_id, command, json.dumps(inputs or {}, ensure_ascii=False, sort_keys=True), iso_utc(utc_now())),
    )
    try:
        yield run
    except Exception as exc:
        conn.execute(
            "UPDATE run_log SET finished_at=?, status='failed', detail_json=?, retry_scope=? WHERE run_id=?",
            (iso_utc(utc_now()), json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), run.retry_scope, run.run_id),
        )
        raise
    else:
        conn.execute(
            "UPDATE run_log SET finished_at=?, status=?, detail_json=?, retry_scope=? WHERE run_id=?",
            (iso_utc(utc_now()), run.status, json.dumps(run.detail, ensure_ascii=False, sort_keys=True), run.retry_scope, run.run_id),
        )
