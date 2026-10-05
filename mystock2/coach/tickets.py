"""操作单：冻结、版本链、唯一选择规则（实施方案 WP6.3–6.5、§6A.1–6A.3）。

- 冻结后不可改（触发器）；内容相同的重复冻结是 no-op（保留首次时间）；内容变化＝新版本并 `supersedes` 指向同一单元内上一张。
- 冻结晚于项目截止：**不回填为正式单**，记一行 `status='missed_deadline'` 的 SKIP（覆盖率可统计）。
- 缺关键数据：`status='unavailable'`，`orders` 为空，旧单带过期标识，**不用旧单冒充新单**。
- 选择单元＝(batch, line, kind, market, target_session, code)；正式评分采用「截止前最后一个成功冻结且已记录 visible_at 的版本」；
  该版本的 `state_ref` 与当前线内状态不符（持仓/现金已变）则**失效**，按「无订单」处理并释放预留（§6A.3）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from mystock2.coach.decide import SKIP, TicketDraft
from mystock2.core.db import atomic
from mystock2.core.money import to_db
from mystock2.core.timeutil import EvidenceTimes, check_time_chain, ensure_utc, iso_utc

KINDS = ("line_sim", "live_guidance")
STAGES = ("close", "preopen", "human_plan")


class TicketError(ValueError):
    pass


@dataclass(frozen=True)
class Cell:
    batch_id: str
    line_id: str
    kind: str
    market: str
    target_session: str
    code: str


def _canonical(c: Cell, d: TicketDraft, *, status: str, stage: str, strategy_version: str, protocol_version: str, state_ref_type: str, state_ref: str,
               deadline_at, valid_from, valid_to) -> dict:
    return {
        "batch_id": c.batch_id, "line_id": c.line_id, "kind": c.kind, "market": c.market, "code": c.code, "target_session": c.target_session,
        "stage": stage, "status": status, "action": d.action, "limit_price": to_db(d.limit_price) if d.limit_price is not None else None,
        "qty": str(d.qty) if d.qty is not None else None, "lot_size": d.lot_size,
        "reserved_cash": to_db(d.reserved_cash) if d.reserved_cash is not None else None,
        "valid_from": valid_from, "valid_to": valid_to, "reason": list(d.reason_codes), "invalidate": list(d.invalidate_if),
        "uncertainty": d.uncertainty, "model_ref": d.model_ref, "strategy_version": strategy_version, "protocol_version": protocol_version,
        "state_ref_type": state_ref_type, "state_ref": state_ref, "deadline_at": iso_utc(deadline_at),
    }


def freeze_tickets(conn: sqlite3.Connection, *, batch_id: str, line_id: str, kind: str, market: str, target_session: date, stage: str,
                   drafts: list[TicketDraft], state_ref_type: str, state_ref: str, strategy_version: str, protocol_version: str,
                   generated_at, now, deadline_at, valid_from=None, valid_to=None, unavailable_reason: str | None = None) -> list[str]:
    """冻结一批草稿（通常一个线、一个市场、一个目标日）；返回 ticket_id 列表。

    - `unavailable_reason`：关键数据缺失时传入，所有草稿被替换为 SKIP 且 status='unavailable'（orders 为空）。
    - now > deadline_at：全部记为 missed_deadline 的 SKIP（不回填）。
    """
    if kind not in KINDS or stage not in STAGES:
        raise TicketError("kind/stage 非法")
    now, deadline, gen = ensure_utc(now), ensure_utc(deadline_at), ensure_utc(generated_at)
    status = "frozen"
    if unavailable_reason:
        status = "unavailable"
    elif now > deadline:
        status = "missed_deadline"
    if status == "frozen":
        problems = check_time_chain(EvidenceTimes(gen, gen, gen, now, deadline))      # generated_at ≤ frozen_at ≤ deadline
        if problems:
            raise TicketError("时间链不合规：" + ",".join(problems))
    ids: list[str] = []
    with atomic(conn):
        for d in drafts:
            eff = d
            if status != "frozen":
                reason = unavailable_reason if status == "unavailable" else "missed_deadline"
                eff = TicketDraft(d.code, SKIP, reason_codes=(reason,), model_ref=d.model_ref)
            cell = Cell(batch_id, line_id, kind, market, target_session.isoformat(), d.code)
            body = _canonical(cell, eff, status=status, stage=stage, strategy_version=strategy_version, protocol_version=protocol_version,
                              state_ref_type=state_ref_type, state_ref=state_ref, deadline_at=deadline, valid_from=iso_utc(valid_from) if valid_from else None,
                              valid_to=iso_utc(valid_to) if valid_to else None)
            h = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            tid = h[:24]
            if conn.execute("SELECT 1 FROM ticket WHERE ticket_id=?", (tid,)).fetchone():
                ids.append(tid)                                      # 相同内容重跑：no-op，保留首次冻结时间
                continue
            prev = conn.execute(
                "SELECT ticket_id FROM ticket WHERE batch_id=? AND line_id=? AND kind=? AND market=? AND target_session=? AND code=? ORDER BY visible_at DESC, rowid DESC LIMIT 1",
                (batch_id, line_id, kind, market, cell.target_session, d.code)).fetchone()
            conn.execute(
                "INSERT INTO ticket(ticket_id, batch_id, line_id, kind, market, code, target_session, stage, status, action, limit_price, qty, lot_size, reserved_cash, "
                "valid_from, valid_to, reason_json, invalidate_json, uncertainty_json, model_ref, strategy_version, protocol_version, state_ref_type, state_ref, "
                "generated_at, frozen_at, visible_at, deadline_at, supersedes, frozen_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, batch_id, line_id, kind, market, d.code, cell.target_session, stage, status, eff.action, body["limit_price"], body["qty"], eff.lot_size,
                 body["reserved_cash"], body["valid_from"], body["valid_to"], json.dumps(body["reason"]), json.dumps(body["invalidate"]),
                 json.dumps(body["uncertainty"], sort_keys=True), eff.model_ref, strategy_version, protocol_version, state_ref_type, state_ref,
                 iso_utc(gen), iso_utc(now), iso_utc(now), iso_utc(deadline), prev["ticket_id"] if prev else None, h))
            ids.append(tid)
    return ids


@dataclass(frozen=True)
class Selection:
    ticket: sqlite3.Row | None
    reason: str                 # selected | none_frozen | state_changed | not_visible_before_deadline


def select_ticket(conn: sqlite3.Connection, cell: Cell, *, deadline_at, current_state_ref: str | None = None) -> Selection:
    """正式评分采用的版本：截止前最后一个成功冻结（status='frozen'）且 visible_at ≤ 截止的版本。

    current_state_ref 给出时校验失效条件：版本的 state_ref 与当前线内状态不符 → 失效（无订单、释放预留）。
    """
    deadline = iso_utc(deadline_at)
    rows = conn.execute(
        "SELECT * FROM ticket WHERE batch_id=? AND line_id=? AND kind=? AND market=? AND target_session=? AND code=? AND status='frozen' "
        "ORDER BY visible_at, rowid", (cell.batch_id, cell.line_id, cell.kind, cell.market, cell.target_session, cell.code)).fetchall()
    if not rows:
        return Selection(None, "none_frozen")
    visible = [r for r in rows if r["visible_at"] <= deadline]
    if not visible:
        return Selection(None, "not_visible_before_deadline")
    last = visible[-1]
    if current_state_ref is not None and last["state_ref"] != current_state_ref:
        return Selection(None, "state_changed")
    return Selection(last, "selected")


def coverage(conn: sqlite3.Connection, batch_id: str, line_id: str, kind: str, market: str, sessions: list[date], codes: list[str]) -> dict:
    """CO-02：每个交易日、每个交易仓标的是否都有操作单（含 SKIP 与「错过截止/不可用」）。"""
    total = len(sessions) * len(codes)
    have = 0
    missing = []
    for s in sessions:
        for c in codes:
            n = conn.execute("SELECT COUNT(*) n FROM ticket WHERE batch_id=? AND line_id=? AND kind=? AND market=? AND target_session=? AND code=?",
                             (batch_id, line_id, kind, market, s.isoformat(), c)).fetchone()["n"]
            if n:
                have += 1
            else:
                missing.append((s.isoformat(), c))
    return {"planned": total, "with_ticket": have, "missing": missing}
