"""对账：事件重建的持仓与现金 vs 券商快照（LG-07）。

持仓数量必须逐标的完全一致；现金逐币种差异须在阈值内；待匹配项、不完整 FX 组、碎股一并列出——
**不隐藏未对账项**。差异的「可解释」由人逐项在回执中说明，本模块只负责如实列出。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from mystock2.core.money import dec
from mystock2.ledger.events import open_pending
from mystock2.ledger.projection import incomplete_fx_groups, project

DEFAULT_CASH_TOLERANCE = Decimal("0.01")


@dataclass
class ReconcileReport:
    snapshot_id: str
    position_diffs: list[dict] = field(default_factory=list)
    cash_diffs: list[dict] = field(default_factory=list)
    open_pending: int = 0
    incomplete_fx_groups: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.position_diffs or self.cash_diffs or self.open_pending or self.incomplete_fx_groups)


def reconcile(conn: sqlite3.Connection, account_id: str, snapshot_id: str,
              cash_tolerance: dict[str, Decimal] | Decimal = DEFAULT_CASH_TOLERANCE) -> ReconcileReport:
    snap = conn.execute("SELECT * FROM account_snapshot WHERE snapshot_id=? AND account_id=?", (snapshot_id, account_id)).fetchone()
    if not snap:
        raise ValueError(f"快照不存在：{snapshot_id}")
    proj = project(conn, account_id, as_of=snap["captured_at"])
    rep = ReconcileReport(snapshot_id)
    rep.warnings = list(proj.warnings)
    spos = {r["code"]: dec(r["qty"]) for r in conn.execute("SELECT code, qty FROM snapshot_position WHERE snapshot_id=?", (snapshot_id,))}
    scash = {r["currency"]: dec(r["cash"]) for r in conn.execute("SELECT currency, cash FROM snapshot_cash WHERE snapshot_id=?", (snapshot_id,))}
    for code in sorted(set(spos) | set(proj.positions)):
        a, b = proj.positions.get(code, Decimal(0)), spos.get(code, Decimal(0))
        if a != b:
            rep.position_diffs.append({"code": code, "ledger": str(a), "broker": str(b), "diff": str(a - b)})
    for ccy in sorted(set(scash) | set(proj.cash)):
        a, b = proj.cash.get(ccy, Decimal(0)), scash.get(ccy, Decimal(0))
        tol = cash_tolerance.get(ccy, DEFAULT_CASH_TOLERANCE) if isinstance(cash_tolerance, dict) else cash_tolerance
        if abs(a - b) > tol:
            rep.cash_diffs.append({"currency": ccy, "ledger": str(a), "broker": str(b), "diff": str(a - b), "tolerance": str(tol)})
    rep.open_pending = len(open_pending(conn))
    rep.incomplete_fx_groups = incomplete_fx_groups(conn, account_id)
    return rep
