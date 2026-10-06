"""人类计划（结构化意图）、暴露日志与密封（实施方案 §6A.2）。

流程：展示「该人类线自己的状态」→ 用户记录结构化计划 → 校验与约束处理（截断或拒绝，预注册二选一）→ 冻结 → 同协议撮合。
暴露：**首次可观测暴露（任一通道）即揭示时间**，写不可改写的暴露日志，并锁定此前最后一个合格计划；
揭示之后的修改带 seen_ai=1、`late_record=1`，**不进入 human_plan 线**（属 human_actual）；揭示前没有记录则按「无订单」处理并标 exposed_before_record。
缺失计划＝确定性的「无订单」，计入分母，不补成 HOLD 动机推断。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mystock2.coach.decide import BUY, HOLD, SELL, SKIP, StateView, TicketDraft
from mystock2.core.db import atomic
from mystock2.core.money import dec, floor_to_lots, to_db
from mystock2.core.timeutil import ensure_utc, iso_utc, utc_now
from mystock2.instruments.security_rule import SecurityRule
from mystock2.ledger.fees import FeeRule, estimate, select_rule

ACTIONS = ("BUY", "SELL", "HOLD", "NO_TRADE")


class IntentError(ValueError):
    pass


class IntentRejected(IntentError):
    """违反约束且协议选择「拒绝并要求重填」。"""


@dataclass(frozen=True)
class IntentResult:
    intent_id: str
    qty: int | None
    truncated: bool
    seen_ai: bool
    late_record: bool
    notes: tuple[str, ...]


def reveal(conn: sqlite3.Connection, *, batch_id: str, market: str, target_session: date, channel: str, version_hashes: list[str], at=None,
           note: str | None = None) -> str:
    """写暴露日志（coach show、人工否决包导出、自报的外部暴露等）。返回 exposure_id。"""
    at = iso_utc(at or utc_now())
    body = json.dumps({"b": batch_id, "m": market, "t": target_session.isoformat(), "c": channel, "at": at, "h": sorted(version_hashes)}, sort_keys=True)
    eid = hashlib.sha256(body.encode()).hexdigest()[:24]
    with atomic(conn):
        conn.execute("INSERT OR IGNORE INTO intent_exposure(exposure_id, batch_id, market, target_session, channel, revealed_at, version_hashes, note) VALUES (?,?,?,?,?,?,?,?)",
                     (eid, batch_id, market, target_session.isoformat(), channel, at, json.dumps(sorted(version_hashes)), note))
    return eid


def first_reveal_at(conn: sqlite3.Connection, batch_id: str, market: str, target_session: date) -> str | None:
    """该市场、该目标日的首次揭示时间。**不按批次过滤**（审核 P0-4）：§6A.2 的暴露是「该目标日任一 AI 版本或等价信息」，
    在批次 B1 看过 AI 单后，为 B2 记录的计划同样不是「未看 AI 的独立想法」。`batch_id` 只为保持调用签名。"""
    del batch_id
    r = conn.execute("SELECT MIN(revealed_at) m FROM intent_exposure WHERE market=? AND target_session=?",
                     (market, target_session.isoformat())).fetchone()
    return r["m"]


def record_intent(conn: sqlite3.Connection, *, batch_id: str, line_id: str, market: str, code: str, target_session: date, action: str,
                  limit_price=None, qty: int | None = None, valid_to=None, state: StateView, state_hash: str, now, deadline_at,
                  constraint_handling: str, rule: SecurityRule | None, fee_rules: list[FeeRule], note: str | None = None) -> IntentResult:
    """记录一条结构化人类计划。`state` 必须是该**人类线自己**的开盘前状态（不是真实账户）。"""
    if action not in ACTIONS:
        raise IntentError(f"action 必须是 {ACTIONS}")
    if constraint_handling not in ("truncate", "reject"):
        raise IntentError("constraint_handling 必须是 truncate 或 reject（协议预注册二选一）")
    now_s, deadline = iso_utc(now), ensure_utc(deadline_at)
    reveal_s = first_reveal_at(conn, batch_id, market, target_session)
    late = (reveal_s is not None and reveal_s <= now_s) or ensure_utc(now) > deadline
    seen = reveal_s is not None and reveal_s <= now_s
    notes: list[str] = []
    q = qty
    truncated = False
    px = dec(limit_price) if limit_price is not None else None
    if action in ("BUY", "SELL"):
        if px is None or q is None or q <= 0:
            raise IntentError("BUY/SELL 必须带限价与正数量")
        if rule is None or rule.lot_size is None:
            raise IntentError("证券规则未知：不接受可执行计划（规则未知不出可执行数量）")
        lot = rule.lot_size
        legal = rule.legal_limit(px, action)
        if legal != px:
            if constraint_handling == "reject":
                raise IntentRejected(f"限价 {px} 不是合法价位；最近的保守价位 {legal}")
            notes.append(f"limit_rounded:{px}->{legal}")
            px = legal
        whole = int(floor_to_lots(q, lot))
        if whole != q:
            if constraint_handling == "reject" or whole <= 0:
                raise IntentRejected(f"数量 {q} 不是整手（{lot}）")
            q, truncated = whole, True
            notes.append(f"qty_floored_to_lot:{qty}->{q}")
        if action == "SELL":
            avail = int(state.qty(code))
            if q > avail:
                if constraint_handling == "reject" or avail < lot:
                    raise IntentRejected(f"超卖：计划 {q} > 该人类线可卖 {avail}")
                q, truncated = int(floor_to_lots(avail, lot)), True
                notes.append(f"qty_truncated_to_inventory:{qty}->{q}")
        else:
            rule_fee = select_rule(fee_rules, market, BUY, target_session.isoformat())

            def need(n):
                return Decimal(n) * px + estimate(rule_fee, [(Decimal(n), px)]).fee
            if need(q) > state.tradable_cash():
                if constraint_handling == "reject":
                    raise IntentRejected("超预算：该人类线可交易现金不足")
                n = q
                while n > 0 and need(n) > state.tradable_cash():
                    n -= lot
                if n <= 0:
                    raise IntentRejected("超预算：可交易现金一手也买不起")
                q, truncated = n, True
                notes.append(f"qty_truncated_to_cash:{qty}->{q}")
    else:
        px, q = None, None
    if late:
        notes.append("late_record")
    body = {"b": batch_id, "l": line_id, "m": market, "c": code, "t": target_session.isoformat(), "a": action, "px": to_db(px) if px is not None else None,
            "q": q, "vt": iso_utc(valid_to) if valid_to else None, "sh": state_hash, "at": now_s, "seen": int(seen), "late": int(late), "n": note}
    h = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    iid = h[:24]
    with atomic(conn):
        conn.execute(
            "INSERT OR IGNORE INTO intent(intent_id, batch_id, line_id, market, code, target_session, action, limit_price, qty, valid_to, state_hash, recorded_at, seen_ai, late_record, note, frozen_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, batch_id, line_id, market, code, target_session.isoformat(), action, body["px"], str(q) if q is not None else None, body["vt"], state_hash,
             now_s, int(seen), int(late), note, h))
    return IntentResult(iid, q, truncated, seen, late, tuple(notes))


def select_human_plan(conn: sqlite3.Connection, *, batch_id: str, line_id: str, market: str, target_session: date, codes: list[str],
                      deadline_at) -> dict[str, dict]:
    """human_plan 线的正式计划：每个标的取「截止前且首次揭示前」记录的最后一个版本（late_record=0）。

    缺失 → {'action':'NO_ORDER','flags':['plan_missing']}（确定性的无订单规则，计入分母）；
    揭示前无记录但之后有补录 → 标 exposed_before_record（补录不进入该线）。
    """
    deadline_s = iso_utc(deadline_at)
    reveal_s = first_reveal_at(conn, batch_id, market, target_session)
    out: dict[str, dict] = {}
    for code in codes:
        rows = conn.execute(
            "SELECT * FROM intent WHERE batch_id=? AND line_id=? AND market=? AND target_session=? AND code=? ORDER BY recorded_at, rowid",
            (batch_id, line_id, market, target_session.isoformat(), code)).fetchall()
        eligible = [r for r in rows if not r["late_record"] and r["recorded_at"] <= deadline_s and (reveal_s is None or r["recorded_at"] < reveal_s)]
        if eligible:
            r = eligible[-1]
            out[code] = {"action": r["action"], "limit_price": r["limit_price"], "qty": r["qty"], "intent_id": r["intent_id"], "flags": [],
                         "valid_to": r["valid_to"], "state_hash": r["state_hash"]}
        else:
            flags = ["plan_missing"]
            if reveal_s is not None and reveal_s <= deadline_s:     # 首次揭示早于任何合格记录（含「揭示后从未补录」）
                flags.append("exposed_before_record")
            out[code] = {"action": "NO_ORDER", "limit_price": None, "qty": None, "intent_id": None, "flags": flags, "valid_to": None, "state_hash": None}
    return out


def plan_to_drafts(plan: dict[str, dict], lot_sizes: dict[str, int]) -> list[TicketDraft]:
    """把选定的计划转成操作单草稿（stage='human_plan' 冻结用）。"""
    drafts = []
    for code, p in plan.items():
        if p["action"] in ("BUY", "SELL"):
            drafts.append(TicketDraft(code, BUY if p["action"] == "BUY" else SELL, dec(p["limit_price"]), int(p["qty"]), lot_sizes.get(code, 1), None,
                                      ("human_plan",), uncertainty={"intent_id": p["intent_id"]}))
        elif p["action"] == "HOLD":
            drafts.append(TicketDraft(code, HOLD, reason_codes=("human_plan_hold",), uncertainty={"intent_id": p["intent_id"]}))
        elif p["action"] == "NO_TRADE":
            drafts.append(TicketDraft(code, SKIP, reason_codes=("human_plan_no_trade",), uncertainty={"intent_id": p["intent_id"]}))
        else:
            drafts.append(TicketDraft(code, SKIP, reason_codes=tuple(p["flags"])))
    return drafts
