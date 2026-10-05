"""操作单视图（M6 WP6.9；密封语义见实施方案 §6A.2、§6A.3，实现见 `web/sealing.py`）。

**密封是硬要求**：对某个（批次、市场、目标日），只有 `intent_exposure` 里已存在揭示记录时，才返回 AI 单的动作、限价、数量、原因码、
不确定性与哈希；**未揭示时只返回「已密封」状态与计数（张数、阶段、状态）**——响应里没有这些字段，也没有能反推动作的内容
（原因码如 `edge_ok`/`no_edge` 本身就泄露动作，因此同样不返回）。选择规则与记分牌引擎共用 `coach.tickets.select_ticket`（整组原子选择：截止前最后一个已冻结且可见的整组，组内取该标的的单）。
人类线（`human_plan`）与 `live_guidance` 不在此视图展示；`live_guidance` 只显示「未提供」占位（M6 未实现）。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date

from mystock2.coach.tickets import Cell, select_ticket
from mystock2.core.timeutil import ensure_utc, iso_utc
from mystock2.instruments.code_map import CodeError, currency_of
from mystock2.web import common as C
from mystock2.web import sealing

ACTION_TEXT = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有", "SKIP": "不操作"}
STATUS_TEXT = {"frozen": "已冻结", "missed_deadline": "错过截止", "unavailable": "不可用（缺关键数据）"}
EFFECTIVE_TEXT = {
    "selected": "生效（唯一选择规则采用）", "state_changed": "失效（线内状态哈希已变，按无订单处理）", "none_frozen": "无已冻结的单",
    "not_visible_before_deadline": "截止前没有可见版本（不采用）", "not_in_latest_group": "最新一组没有该标的的单（整组原子选择，不拼接旧组，按无订单处理）",
    "expired": "已过有效期（按无订单处理）", "unchecked": "生效（线内状态哈希未能校验）",
}
SEALED_TEXT = "已密封"
LIVE_GUIDANCE = {"status": "未提供", "text": C.text_cell("未提供", tag="M6 未实现"),
                 "note": "live_guidance（真实账户指导单）依赖真实账户授权与采集，尚未实现；它不进入正式评分，只用于展示与执行验证。"}


def _ccy(code: str) -> str | None:
    try:
        return currency_of(code)
    except CodeError:
        return None


def _batch_meta(row) -> dict:
    try:
        return json.loads(row["initial_state_json"]).get("_meta", {}) or {}
    except (ValueError, TypeError):
        return {}


def _current_state_hash(conn, batch_id: str, line_id: str, target: str) -> str | None:
    """该批次最近一次记分牌 run 里，该线在目标日开盘前的线内状态哈希；没有 run 则 None（无法校验）。"""
    r = conn.execute("SELECT ls.state_hash FROM line_state ls JOIN eval_run er ON er.run_id = ls.run_id WHERE er.batch_id=? AND ls.line_id=? AND ls.date=? "
                     "ORDER BY er.created_at DESC, ls.run_id DESC LIMIT 1", (batch_id, line_id, target)).fetchone()
    return r["state_hash"] if r else None


def _sealed_block(rows, planned_cells: int | None) -> dict:
    """未揭示：只给计数。这里刻意不读取 action/limit_price/qty/reason_json/uncertainty_json/frozen_hash 等任何内容字段。"""
    cells: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        cells.setdefault((r["line_id"], r["code"]), []).append(r["status"])
    by_status = Counter(r["status"] for r in rows)
    return {
        "tickets": len(rows), "cells": len(cells), "ai_lines": len({r["line_id"] for r in rows}),
        "by_stage": dict(sorted(Counter(r["stage"] for r in rows).items())),
        "by_status": {k: by_status.get(k, 0) for k in ("frozen", "missed_deadline", "unavailable")},
        "coverage": {"planned_cells": planned_cells, "cells_with_ticket": len(cells),
                     "cells_with_frozen": sum(1 for s in cells.values() if "frozen" in s)},
    }


def _version(r, selected_id: str | None, deadline, ccy: str | None) -> dict:
    after = deadline is not None and ensure_utc(r["visible_at"]) > deadline
    return {"visible_at": r["visible_at"], "stage": r["stage"], "status": r["status"], "status_text": STATUS_TEXT.get(r["status"], r["status"]),
            "action_text": ACTION_TEXT.get(r["action"], r["action"]),
            "limit_price": C.price_cell(r["limit_price"], ccy) if r["limit_price"] is not None else C.na_cell("该版本没有限价"),
            "qty": C.qty_cell(r["qty"]) if r["qty"] is not None else C.na_cell("该版本没有数量"),
            "frozen_hash": r["frozen_hash"], "supersedes": r["supersedes"], "selected": r["ticket_id"] == selected_id, "after_deadline": after}


def _revealed_rows(conn, batch_id: str, market: str, target: str, rows, deadline) -> list[dict]:
    cells: dict[tuple[str, str], list] = {}
    for r in rows:
        cells.setdefault((r["line_id"], r["code"]), []).append(r)
    kinds = sealing.line_kinds(conn)
    out = []
    for (line_id, code), vs in sorted(cells.items()):
        ccy = _ccy(code)
        sel = select_ticket(conn, Cell(batch_id, line_id, "line_sim", market, target, code), deadline_at=deadline) if deadline else None
        t = sel.ticket if sel else None
        reason = sel.reason if sel else "none_frozen"
        cur = _current_state_hash(conn, batch_id, line_id, target) if t is not None else None
        if t is not None and cur is None:
            reason = "unchecked"
        elif t is not None and cur != t["state_ref"]:
            reason = "state_changed"
        visible = [v for v in vs if v["status"] == "frozen" and deadline is not None and ensure_utc(v["visible_at"]) <= deadline]
        shown = t or (visible[-1] if visible else vs[-1])     # 没有可采用的版本时，展示截止前最近的已冻结版本（或最近一个 SKIP 行）
        base = {
            "line_id": line_id, "line_kind": kinds.get(line_id) or line_id.rsplit(":", 1)[-1], "code": code, "currency": ccy, "stage": shown["stage"],
            "status": shown["status"], "status_text": STATUS_TEXT.get(shown["status"], shown["status"]),
            "effective": {"code": reason, "text": EFFECTIVE_TEXT.get(reason, f"不采用（{reason}）")},
            "versions": [_version(v, t["ticket_id"] if t is not None else None, deadline, ccy) for v in vs], "version_count": len(vs),
        }
        action = C.text_cell(ACTION_TEXT.get(shown["action"], shown["action"]), tag="已失效" if reason == "state_changed" else None)
        base.update({
            "action": action, "action_code": shown["action"],
            "limit_price": C.price_cell(shown["limit_price"], ccy) if shown["limit_price"] is not None else C.na_cell("该单没有限价"),
            "qty": C.qty_cell(shown["qty"]) if shown["qty"] is not None else C.na_cell("该单没有数量"),
            "lot_size": shown["lot_size"], "valid_from": shown["valid_from"], "valid_to": shown["valid_to"],
            "reserved_cash": C.money_cell(shown["reserved_cash"], ccy) if shown["reserved_cash"] is not None and ccy else C.na_cell("该单没有预留资金"),
            "reasons": json.loads(shown["reason_json"]), "invalidate_if": json.loads(shown["invalidate_json"]),
            "uncertainty": json.loads(shown["uncertainty_json"]), "deadline_at": shown["deadline_at"], "generated_at": shown["generated_at"],
            "frozen_at": shown["frozen_at"], "visible_at": shown["visible_at"], "state_ref": shown["state_ref"], "state_ref_type": shown["state_ref_type"],
            "frozen_hash": shown["frozen_hash"], "strategy_version": shown["strategy_version"], "protocol_version": shown["protocol_version"],
            "current_state_hash": cur,
        })
        out.append(base)
    return out


def run(conn, params):
    now = C.now_of(params)
    batches = conn.execute("SELECT * FROM comparison_batch ORDER BY created_at DESC, batch_id").fetchall()
    if not batches:
        raise C.ViewUnavailable("no_batch", "还没有比较批次：尚未运行 batch create（比较批次由受控 CLI 写入，Web 只读）")
    want = (params.get("batch") or "").strip()
    row = next((b for b in batches if b["batch_id"] == want), None) if want else None
    if want and row is None:
        raise C.ViewUnavailable("batch_not_found", f"批次不存在：{want}")
    if row is None:
        have = {r["batch_id"] for r in sealing.ai_ticket_rows(conn)}
        row = next((b for b in batches if b["batch_id"] in have), batches[0])
    batch_id = row["batch_id"]
    meta = _batch_meta(row)
    target_arg = (params.get("target") or "").strip()
    if target_arg:
        try:
            target_arg = date.fromisoformat(target_arg).isoformat()
        except ValueError:
            raise C.ViewUnavailable("bad_target", f"目标日格式应为 YYYY-MM-DD：{target_arg!r}") from None

    all_rows = sealing.ai_ticket_rows(conn, batch_id=batch_id)
    markets = sorted({r["market"] for r in all_rows})
    if params.get("market"):
        markets = [m for m in markets if m == params["market"]]
    ai_lines = sorted({r["line_id"] for r in all_rows})
    entries = []
    for m in markets:
        mrows = [r for r in all_rows if r["market"] == m]
        target = target_arg or max(r["target_session"] for r in mrows)
        rows = [r for r in mrows if r["target_session"] == target]
        deadline = sealing.deadline_of(m, target)
        reveal = sealing.reveal_info(conn, batch_id, m, target, now)
        codes = meta.get("codes") if isinstance(meta.get("codes"), list) else None
        planned = len(codes) * len({r["line_id"] for r in mrows}) if codes is not None else None
        entry = {"market": m, "target_session": target, "deadline_at": iso_utc(deadline) if deadline else None,
                 "state": "revealed" if reveal else "sealed", "state_text": "已揭示" if reveal else SEALED_TEXT, "reveal": reveal}
        if reveal is None:
            entry.update({"counts": _sealed_block(rows, planned), "rows": []})
        else:
            revealed_rows = _revealed_rows(conn, batch_id, m, target, rows, deadline)
            counts = _sealed_block(rows, planned)
            entry.update({"counts": counts, "rows": revealed_rows})
        entries.append(entry)

    vis = [r for r in all_rows]
    srcs = [C.source("操作单（冻结记录）", max((r["generated_at"] for r in vis), key=ensure_utc) if vis else None,
                     max((r["visible_at"] for r in vis), key=ensure_utc) if vis else None)]
    notes = ["密封：未揭示的目标日只显示「已密封」与计数；揭示只能经 CLI（coach show、人工否决包导出）写入暴露日志，Web 不提供揭示入口",
             "只展示 AI 线的 line_sim 单；人类线（human_plan）与 live_guidance 不在此展示"]
    if not all_rows:
        notes.append("该批次还没有任何 AI 单")
    return {
        "batch_id": batch_id, "batches": [b["batch_id"] for b in batches], "batch_currency": row["currency"], "batch_start": row["start_date"],
        "ai_lines": ai_lines, "markets": entries, "live_guidance": LIVE_GUIDANCE, "now": iso_utc(now),
        "_freshness": C.freshness(srcs, notes),
    }
