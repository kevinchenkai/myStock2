"""从账本（只读连接）整理成交、费用归属与盈亏计算的输入。

读取经 `mystock2.ledger.projection`（有效版本、有效排序）；盈亏本身由纯函数 `mystock2.ledger.pnl` 计算。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc
from mystock2.ledger.pnl import BUY, OPENING, SELL, SPLIT, TradeEvent
from mystock2.ledger.projection import effective_events


@dataclass
class LedgerTrades:
    account_id: str
    opening_at: str | None
    opening_snapshot_id: str | None
    fills: list[dict] = field(default_factory=list)
    unattributed_fees: list[dict] = field(default_factory=list)
    trade_events: list[TradeEvent] = field(default_factory=list)
    codes: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _fee_kind(row) -> str:
    bk = row["business_key"]
    return bk.rsplit(":", 1)[-1] if bk.startswith("fee:") else row["event_type"].lower()


def _opening_cost_evidence(conn, account_id, op, events, t0) -> dict[str, Decimal]:
    """开账持仓的每股成本证据：**平均成本（average_cost）**，不是摊薄成本（cost_price 可为负，首跑核实）。

    优先用开账快照自带的 average_cost；没有则用开账后**最早**一份带 average_cost 的券商快照，且要求该标的在开账点与该快照之间没有成交
    （否则平均成本已被改变，不能代表开账时的成本）。都没有 → 不给成本（不猜）。
    开账点与该快照之间登记过拆股／并股时，快照的平均成本是拆股后的每股成本：按拆股因子换回开账时的每股成本
    （pnl 会在拆股时刻再按因子调整，审核 P1-2）。
    """
    out: dict[str, Decimal] = {}
    if not op or t0 is None:
        return out
    fill_times: dict[str, list] = {}
    for e in events:
        if e["event_type"] == "FILL":
            fill_times.setdefault(e["code"], []).append(ensure_utc(e["event_at"]))
    splits: dict[str, list[tuple]] = {}
    for r in conn.execute("SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT'"):
        splits.setdefault(r["code"], []).append((ensure_utc(r["effective_at"]), r["ratio_num"], r["ratio_den"]))
    snaps = conn.execute("SELECT snapshot_id, captured_at FROM account_snapshot WHERE account_id=? AND source!='v1-date-only' AND captured_at>=? "
                         "ORDER BY captured_at, snapshot_id", (account_id, op["opening_at"])).fetchall()
    for s in snaps:
        cap = ensure_utc(s["captured_at"])
        for r in conn.execute("SELECT code, average_cost FROM snapshot_position WHERE snapshot_id=? AND average_cost IS NOT NULL", (s["snapshot_id"],)):
            code = r["code"]
            if code in out or dec(r["average_cost"]) <= 0:
                continue
            if any(t0 < t <= cap for t in fill_times.get(code, ())):
                continue
            cost = dec(r["average_cost"])
            for eff, num, den in splits.get(code, ()):
                if t0 < eff <= cap:
                    cost = cost * Decimal(num) / Decimal(den)
            out[code] = cost
    return out


def load_trades(conn: sqlite3.Connection, account_id: str) -> LedgerTrades:
    op = conn.execute("SELECT opening_at, snapshot_id FROM account_opening WHERE account_id=?", (account_id,)).fetchone()
    out = LedgerTrades(account_id, op["opening_at"] if op else None, op["snapshot_id"] if op else None)
    if op is None:
        out.warnings.append("no_opening")
    t0 = ensure_utc(op["opening_at"]) if op else None
    events = effective_events(conn, account_id)

    versions: dict[str, int] = {}
    for r in conn.execute("SELECT business_key, COUNT(*) AS n FROM ledger_event WHERE account_id=? GROUP BY business_key", (account_id,)):
        versions[r["business_key"]] = r["n"]
    srcs: dict[str, set[str]] = {}
    for r in conn.execute(
            "SELECT e.business_key AS bk, l.source_record_id AS sid FROM source_link l JOIN ledger_event e ON e.event_id=l.event_id "
            "WHERE e.account_id=?", (account_id,)):
        srcs.setdefault(r["bk"], set()).add(r["sid"])

    fees: dict[str, list[dict]] = {}
    for e in events:
        if e["event_type"] in ("FEE", "TAX") and e["ref_deal_id"]:
            fees.setdefault(e["ref_deal_id"], []).append({"kind": _fee_kind(e), "type": e["event_type"], "amount": -dec(e["cash_delta"]),
                                                          "currency": e["currency"], "event_id": e["event_id"]})
    fill_deals = {e["ref_deal_id"] for e in events if e["event_type"] == "FILL" and e["ref_deal_id"]}
    for deal, items in fees.items():
        if deal not in fill_deals:
            out.unattributed_fees.extend({"deal_id": deal, **i} for i in items)

    cost_by_code = _opening_cost_evidence(conn, account_id, op, events, t0)

    for e in events:
        t = e["event_type"]
        if t == "OPENING_POSITION":
            out.codes.add(e["code"])
            out.trade_events.append(TradeEvent(OPENING, e["code"], e["currency"], e["event_at"], dec(e["qty_delta"]),
                                               cost_by_code.get(e["code"]), Decimal(0), e["event_id"]))
        elif t == "FILL":
            qty = dec(e["qty_delta"])
            fee_items = fees.get(e["ref_deal_id"], []) if e["ref_deal_id"] else []
            # 费用只在成交币种内合计；币种不同的费用（异常数据）单列，不并入、不改标币种（审核 P1-7）
            fee_total = sum((i["amount"] for i in fee_items if i["currency"] == e["currency"]), Decimal(0))
            fee_other = [i for i in fee_items if i["currency"] != e["currency"]]
            if fee_other:
                out.warnings.append(f"fee_currency_mismatch:{e['ref_deal_id']}")
            pre = t0 is not None and ensure_utc(e["event_at"]) <= t0
            out.codes.add(e["code"])
            out.fills.append({
                "event_id": e["event_id"], "business_key": e["business_key"], "deal_id": e["ref_deal_id"], "order_id": e["ref_order_id"],
                "code": e["code"], "currency": e["currency"], "event_at": e["event_at"], "received_at": e["received_at"],
                "side": "BUY" if qty > 0 else "SELL", "qty": abs(qty), "price": dec(e["price"]), "cash_delta": dec(e["cash_delta"]),
                "fees": fee_items, "fee_total": fee_total, "fee_other_ccy": fee_other, "sources": len(srcs.get(e["business_key"], ())),
                "versions": versions.get(e["business_key"], 1), "pre_opening": pre,
            })
            out.trade_events.append(TradeEvent(BUY if qty > 0 else SELL, e["code"], e["currency"], e["event_at"], abs(qty),
                                               dec(e["price"]), fee_total, e["ref_deal_id"] or e["event_id"]))
    if out.codes:
        marks = ",".join("?" * len(out.codes))
        for r in conn.execute(f"SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT' AND code IN ({marks})",
                              sorted(out.codes)):
            out.trade_events.append(TradeEvent(SPLIT, r["code"], "", r["effective_at"], ratio_num=r["ratio_num"], ratio_den=r["ratio_den"]))
    return out
