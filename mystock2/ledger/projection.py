"""由事件重建持仓、逐币种现金与应收（实施方案 §6 账本不变量）。

- 和式从开账点 t0 起：`event_at <= t0` 的历史事件标 pre_opening，只作描述，不参与前向和式（不变量 1）。
- 持仓＝Σ 原始数量 × Π（有效时点在该事件之后且 ≤ as_of 的拆股因子）；拆股是因子不是数量增量（不变量 2）。
  同一时刻先应用公司行动因子、再处理该时刻成交：因子只作用于 `effective_at > event_at` 的事件。
- 更正（REVERSAL＋新版本）自然体现在和式里；有效排序＝(event_at, business_key, event_version)。
- 权益（含价格）在 M5 计算：必须用**未复权**收盘价，复权价只用于特征（§6A.5）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from mystock2.core.money import dec
from mystock2.core.timeutil import iso_utc

OPENING_TYPES = ("OPENING_POSITION", "OPENING_CASH")
EXTERNAL_TYPES = ("DEPOSIT", "WITHDRAW")


@dataclass
class Projection:
    account_id: str
    as_of: str | None
    opening_at: str | None
    positions: dict[str, Decimal] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)
    receivable: dict[str, Decimal] = field(default_factory=dict)
    external_flow: dict[str, Decimal] = field(default_factory=dict)
    fees: dict[str, Decimal] = field(default_factory=dict)       # FEE（负数为支出）
    taxes: dict[str, Decimal] = field(default_factory=dict)      # TAX
    attributed_shortfall: dict[str, Decimal] = field(default_factory=dict)   # 非现金归因
    pre_opening_events: int = 0
    warnings: list[str] = field(default_factory=list)


def _add(d: dict[str, Decimal], k: str, v: Decimal) -> None:
    d[k] = d.get(k, Decimal(0)) + v


def effective_order_key(row) -> tuple:
    return (row["event_at"], row["business_key"], row["event_version"])


def load_events(conn: sqlite3.Connection, account_id: str, as_of=None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM ledger_event WHERE account_id=?"
    args: list = [account_id]
    if as_of is not None:
        sql += " AND event_at <= ?"
        args.append(iso_utc(as_of))
    rows = conn.execute(sql, args).fetchall()
    return sorted(rows, key=effective_order_key)


def _splits(conn, as_of) -> dict[str, list[tuple[str, int, int]]]:
    out: dict[str, list[tuple[str, int, int]]] = {}
    sql = "SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT'"
    args: list = []
    if as_of is not None:
        sql += " AND effective_at <= ?"
        args.append(iso_utc(as_of))
    for r in conn.execute(sql + " ORDER BY effective_at", args):
        out.setdefault(r["code"], []).append((r["effective_at"], r["ratio_num"], r["ratio_den"]))
    return out


def project(conn: sqlite3.Connection, account_id: str, as_of=None) -> Projection:
    opening = conn.execute("SELECT opening_at FROM account_opening WHERE account_id=?", (account_id,)).fetchone()
    t0 = opening["opening_at"] if opening else None
    t_open = t0
    p = Projection(account_id, iso_utc(as_of) if as_of is not None else None, t0)
    if t0 is None:
        p.warnings.append("no_opening")
    events = load_events(conn, account_id, as_of)
    splits = _splits(conn, as_of)
    bad_fx = set(incomplete_fx_groups(conn, account_id))              # 缺腿/不合规的 FX 组整组不生效（不变量 8）
    for g in sorted(bad_fx):
        p.warnings.append(f"fx_group_incomplete:{g}")
    with localcontext() as ctx:
        ctx.prec = 40
        for e in events:
            t = e["event_type"]
            if t == "REVERSAL":                      # 按被冲销事件的类型归类（note 形如 "reverses FILL#1"）
                t = (e["note"] or "").split()[1].split("#")[0]
            if e["group_id"] in bad_fx and t == "FX":
                continue
            if t_open is not None and t not in OPENING_TYPES and e["event_at"] <= t_open:      # 开账边界按「被冲销事件」的类型判断
                p.pre_opening_events += 1
                continue
            ccy = e["currency"]
            cash, recv, qty = dec(e["cash_delta"]), dec(e["recv_delta"]), dec(e["qty_delta"])
            if cash:
                _add(p.cash, ccy, cash)
                if t in EXTERNAL_TYPES or (t == "ADJUST" and e["adjust_class"] == "EXTERNAL_FLOW"):
                    _add(p.external_flow, ccy, cash)
                elif t == "FEE":
                    _add(p.fees, ccy, cash)
                elif t == "TAX":
                    _add(p.taxes, ccy, cash)
            if recv:
                _add(p.receivable, ccy, recv)
            if t == "DIVIDEND_SHORTFALL" and e["attrib_amount"]:
                _add(p.attributed_shortfall, ccy, dec(e["attrib_amount"]))
            if qty:
                q = qty
                for eff_at, num, den in splits.get(e["code"], []):
                    if eff_at > e["event_at"]:          # 同一时刻不缩放：先应用公司行动，再处理成交
                        q = q * num / den
                _add(p.positions, e["code"], q)
    p.positions = {k: v for k, v in p.positions.items() if v != 0}
    p.cash = {k: v for k, v in p.cash.items() if v != 0}
    p.receivable = {k: v for k, v in p.receivable.items() if v != 0}
    for code, q in p.positions.items():
        if q != q.to_integral_value():
            p.warnings.append(f"fractional_position:{code}")
    return p


def incomplete_fx_groups(conn: sqlite3.Connection, account_id: str) -> list[str]:
    """有效腿不是恰好两条（币种不同、方向相反）的 FX 组：缺一腿则整组不应生效。"""
    bad = []
    rows = conn.execute(
        "SELECT group_id, leg_id, currency, cash_delta, event_type FROM ledger_event WHERE account_id=? AND group_id IS NOT NULL "
        "AND (event_type='FX' OR (event_type='REVERSAL' AND note LIKE 'reverses FX%')) ORDER BY group_id", (account_id,)).fetchall()
    groups: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        g = groups.setdefault(r["group_id"], {})
        key = f"{r['leg_id']}:{r['currency']}"
        g[key] = g.get(key, Decimal(0)) + dec(r["cash_delta"])
    for gid, legs in groups.items():
        live = {k: v for k, v in legs.items() if v != 0}
        if live and (len(live) != 2 or len({k.split(':')[1] for k in live}) != 2 or (sum(1 for v in live.values() if v > 0) != 1)):
            bad.append(gid)
    return bad


def effective_events(conn: sqlite3.Connection, account_id: str) -> list[sqlite3.Row]:
    """每个业务键的当前有效版本（最后版本且非 REVERSAL），按有效排序。"""
    rows = load_events(conn, account_id)
    last: dict[str, sqlite3.Row] = {}
    for r in rows:
        cur = last.get(r["business_key"])
        if cur is None or r["event_version"] > cur["event_version"]:
            last[r["business_key"]] = r
    return sorted((r for r in last.values() if r["event_type"] != "REVERSAL"), key=effective_order_key)
