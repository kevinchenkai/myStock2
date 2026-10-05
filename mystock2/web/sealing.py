"""操作单密封与暴露（实施方案 §6A.2、§6A.3）：Web 与 API 的共用判断。

规则（硬性）：
- 只有当（批次、市场、目标日）在 `intent_exposure` 中**已存在揭示记录**（`revealed_at` 不晚于当前时间）时，才可返回该日 AI 单的
  动作/限价/数量/原因码/不确定性/哈希；否则只能返回「已密封」状态与计数（张数、阶段、状态）。
- 揭示只由 CLI 写入（`coach show`、人工否决包导出等）；Web 只读，不提供揭示入口。
- 只展示 AI 线（kind ∈ ai/ai_veto/ai_lgbm）的 `line_sim` 单；人类线（`human_plan`）与 `live_guidance` 不在此展示。
- 选择规则复用 `coach.tickets.select_ticket`：截止前最后一个成功冻结且 `visible_at ≤ 截止` 的版本，与记分牌引擎采用同一函数。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from mystock2.core import calendars as cal
from mystock2.core.timeutil import ensure_utc

AI_KINDS = ("ai", "ai_veto", "ai_lgbm")


def line_kinds(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["line_id"]: r["kind"] for r in conn.execute("SELECT line_id, kind FROM strategy_line")}


def is_ai_line(line_id: str, kinds: dict[str, str]) -> bool:
    """AI 线：登记的类型属于 AI_KINDS；未登记的线按 `批次:类型` 命名推断；线 id 含 human_plan/human_actual 一律不是。"""
    if "human_plan" in line_id or "human_actual" in line_id:
        return False
    kind = kinds.get(line_id) or line_id.rsplit(":", 1)[-1]
    return kind in AI_KINDS


def reveal_info(conn: sqlite3.Connection, batch_id: str, market: str, target_session: str, now: datetime) -> dict | None:
    """该（批次、市场、目标日）的揭示记录摘要；没有（或记录时间晚于当前时间，保守视为未揭示）则 None。"""
    now = ensure_utc(now)
    rows = conn.execute("SELECT exposure_id, channel, revealed_at FROM intent_exposure WHERE batch_id=? AND market=? AND target_session=? "
                        "ORDER BY revealed_at, exposure_id", (batch_id, market, target_session)).fetchall()
    rows = [r for r in rows if ensure_utc(r["revealed_at"]) <= now]
    if not rows:
        return None
    return {"count": len(rows), "first_revealed_at": min((r["revealed_at"] for r in rows), key=ensure_utc),
            "channels": sorted({r["channel"] for r in rows})}


def ai_ticket_rows(conn: sqlite3.Connection, *, batch_id: str | None = None, market: str | None = None, target: str | None = None,
                   statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
    """AI 线 line_sim 单的全部版本（只做行过滤，不含 live_guidance 与人类线）。**调用方负责按密封状态决定能否对外返回内容。**"""
    sql, args = "SELECT * FROM ticket WHERE kind='line_sim'", []
    for col, val in (("batch_id", batch_id), ("market", market), ("target_session", target)):
        if val:
            sql += f" AND {col}=?"
            args.append(val)
    if statuses:
        sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    kinds = line_kinds(conn)
    return [r for r in conn.execute(sql + " ORDER BY visible_at, rowid", args) if is_ai_line(r["line_id"], kinds)]


def deadline_of(market: str, target_session: str) -> datetime | None:
    try:
        return cal.project_deadline(market, target_session)
    except (cal.CalendarError, KeyError, ValueError):
        return None


def latest_ai_status_by_code(conn: sqlite3.Connection, now: datetime) -> dict[str, dict]:
    """每个标的在其市场「最近目标日」是否已有已冻结的 AI 单，以及（只是）是否已揭示——**不含动作、不含任何内容**。

    返回 {code: {"target": 日期, "state": "sealed"|"revealed"}}；没有已冻结单的标的不在结果里。
    同一标的在多个批次都有单时：任一批次未揭示 → sealed（保守）；全部已揭示才 revealed。
    """
    rows = ai_ticket_rows(conn, statuses=("frozen",))
    latest: dict[str, str] = {}
    for r in rows:
        if r["target_session"] > latest.get(r["market"], ""):
            latest[r["market"]] = r["target_session"]
    cells: dict[str, set[tuple[str, str, str]]] = {}
    for r in rows:
        if r["target_session"] == latest.get(r["market"]):
            cells.setdefault(r["code"], set()).add((r["batch_id"], r["market"], r["target_session"]))
    out: dict[str, dict] = {}
    for code, cs in cells.items():
        revealed = all(reveal_info(conn, b, m, t, now) is not None for b, m, t in cs)
        out[code] = {"target": max(t for _, _, t in cs), "state": "revealed" if revealed else "sealed"}
    return out
