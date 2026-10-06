"""V1 历史数据一次性、**只读**导入（实施方案 §3.1、WP2.5/M2b；LG-08）。

- 只用 `mode=ro` + `query_only` 打开 V1 库；**不写 V1、不改 V1、不运行 V1 代码**。真实导入须负责人授权（M0b）。
- V1 `deals` → 规范 FILL 事件：规范键与 Futu 直采**相同**（`fill:{account}:{deal_id}`，不含来源），故同一笔成交跨通道自然归并；
  V1 历史行**没有账户标识**，必须由调用方显式给出 `account_id`（遗留账户占位须与将来 Futu 采集用同一个，才能去重）。
- V1 的成交价/数量是 REAL（float64），不能还原十进制原文：入库按 4 位小数（价格）/6 位小数（数量）量化，**逐笔记录量化舍入差**。
- V1 订单/成交时间是交易所本地时间且**无时区标记**：按市场补 IANA 时区并在报告里标「推断」。
- V1 没有费用、资金流水、股息：这些**不会**被凭空补出；报告里如实列出缺口。开账日前历史仅作描述（账本规则已保证）。
- V1 `positions`/`account_funds` 日快照 → `account_snapshot`（仅有日期，无采集时刻：`captured_at` 取当日 23:59:59Z，source='v1-date-only'）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from mystock2.core.db import ro_uri
from mystock2.core.timeutil import MARKET_TZ, iso_utc
from mystock2.instruments.code_map import CodeError, market_of
from mystock2.ledger.events import (
    EventDraft,
    IdentityInsufficient,
    LedgerConflict,
    LedgerError,
    SourceDraft,
    ensure_account,
    fill_key,
    post_event,
    queue_pending,
)
from mystock2.ledger.opening import create_snapshot

PRICE_Q = Decimal("0.0001")
QTY_Q = Decimal("0.000001")
V1_TABLES = {"deals", "orders", "positions", "account_funds"}


class V1Error(RuntimeError):
    pass


@dataclass
class ImportReport:
    deals_read: int = 0
    inserted: int = 0
    duplicate: int = 0
    conflicts: list[str] = field(default_factory=list)
    pending: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)       # (deal_id, 原因)
    tz_inferred: int = 0
    max_price_rounding: Decimal = Decimal(0)
    total_notional_rounding: Decimal = Decimal(0)
    snapshots: int = 0
    first_deal: str | None = None
    last_deal: str | None = None
    gaps: tuple[str, ...] = ("fees_not_in_v1", "cash_flows_not_in_v1", "dividends_not_in_v1", "account_id_not_in_v1", "timezone_inferred")


def open_v1_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise V1Error(f"V1 数据库不存在：{p}")
    conn = sqlite3.connect(ro_uri(p), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = V1_TABLES - names
    if missing:
        raise V1Error(f"不像 V1 数据库，缺少表：{sorted(missing)}")
    return conn


def _q(x: float | None, step: Decimal) -> tuple[Decimal, Decimal]:
    """float → (量化后的 Decimal, 舍入差绝对值)。"""
    if x is None:
        raise ValueError("缺值")
    exact = Decimal(repr(float(x)))
    q = exact.quantize(step, rounding=ROUND_HALF_EVEN)
    return q, abs(exact - q)


def _local_to_utc(market: str, text: str) -> str:
    t = text.strip().replace("T", " ")
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in t else "%Y-%m-%d %H:%M:%S"
    return iso_utc(datetime.strptime(t, fmt).replace(tzinfo=MARKET_TZ[market]))


def import_deals(v1: sqlite3.Connection, ledger: sqlite3.Connection, *, account_id: str, dry_run: bool = False) -> ImportReport:
    """把 V1 成交导入账本。`ledger` 须是 `ledger` 写连接（dry_run 时只统计，不写）。"""
    rep = ImportReport()
    if not dry_run:
        ensure_account(ledger, account_id, "futu", "REAL", None, note="V1 遗留账户占位（V1 历史行无账户标识；须与将来 Futu 采集使用同一 account_id 才能去重）")
    for r in v1.execute("SELECT * FROM deals ORDER BY create_time, deal_id"):
        rep.deals_read += 1
        deal_id = r["deal_id"]
        try:
            code = r["code"]
            market = market_of(code)
        except CodeError:
            rep.skipped.append((str(deal_id), f"bad_code:{r['code']}"))
            continue
        side = (r["trd_side"] or "").upper()
        if side not in ("BUY", "SELL") or not r["create_time"] or r["price"] is None or r["qty"] is None or not r["qty"]:
            rep.skipped.append((str(deal_id), f"incomplete:{side}"))
            continue
        try:
            price, p_err = _q(r["price"], PRICE_Q)
            qty, q_err = _q(abs(r["qty"]), QTY_Q)
            at = _local_to_utc(market, r["create_time"])
        except (ValueError, TypeError) as exc:
            rep.skipped.append((str(deal_id), f"unparseable:{exc}"))
            continue
        rep.tz_inferred += 1
        rep.max_price_rounding = max(rep.max_price_rounding, p_err)
        rep.total_notional_rounding += p_err * qty + q_err * price
        rep.first_deal = rep.first_deal or at
        rep.last_deal = at
        signed = qty if side == "BUY" else -qty
        ccy = "HKD" if market == "HK" else "USD"
        src = SourceDraft("v1", str(deal_id) if deal_id else f"row:{r['create_time']}:{code}",
                          {"deal_id": deal_id, "order_id": r["order_id"], "code": code, "side": side, "price": repr(float(r["price"])), "qty": repr(float(r["qty"])),
                           "create_time": r["create_time"]})
        if dry_run:
            if deal_id:
                rep.inserted += 1
            else:
                rep.pending += 1
            continue
        try:
            draft = EventDraft(fill_key(account_id, str(deal_id) if deal_id else None), account_id, "FILL", at, ccy, code=code, price=str(price), qty_delta=str(signed),
                               cash_delta=str(-(signed * price)), ref_deal_id=str(deal_id), ref_order_id=r["order_id"],
                               note="v1_import;tz_inferred;quantized(price4,qty6)")
            res = post_event(ledger, draft, source=src)
            if res.status == "inserted":
                rep.inserted += 1
            else:
                rep.duplicate += 1
        except IdentityInsufficient:
            queue_pending(ledger, src, "V1 成交缺少 deal_id")
            rep.pending += 1
        except LedgerConflict as exc:
            rep.conflicts.append(f"{deal_id}: {exc}")
        except LedgerError as exc:
            rep.skipped.append((str(deal_id), f"invalid:{exc}"))
    return rep


def import_snapshots(v1: sqlite3.Connection, ledger: sqlite3.Connection, *, account_id: str, dry_run: bool = False) -> int:
    """V1 日快照 → account_snapshot（只有日期：captured_at=当日 23:59:59Z，source='v1-date-only'）。"""
    funds = {r["snapshot_date"]: r for r in v1.execute("SELECT * FROM account_funds")}
    pos: dict[str, list] = {}
    for r in v1.execute("SELECT * FROM positions"):
        pos.setdefault(r["snapshot_date"], []).append(r)
    n = 0
    for day in sorted(set(funds) | set(pos)):
        positions = {}
        for r in pos.get(day, []):
            try:
                market_of(r["code"])
            except CodeError:
                continue
            if r["qty"] is None:
                continue
            p = {"qty": str(_q(r["qty"], QTY_Q)[0])}
            if r["can_sell_qty"] is not None:
                p["sellable_qty"] = str(_q(r["can_sell_qty"], QTY_Q)[0])
            if r["cost_price"]:
                p["cost_basis"] = str(_q(r["cost_price"], PRICE_Q)[0])
            positions[r["code"]] = p
        cash = {}
        f = funds.get(day)
        if f is not None:
            if f["hk_cash"] is not None:
                cash["HKD"] = {"cash": str(_q(f["hk_cash"], Decimal("0.01"))[0])}
            if f["us_cash"] is not None:
                cash["USD"] = {"cash": str(_q(f["us_cash"], Decimal("0.01"))[0])}
        if not positions and not cash:
            continue
        n += 1
        if not dry_run:
            create_snapshot(ledger, account_id, f"{day}T23:59:59Z", "v1-date-only", positions, cash)
    return n


def run_import(v1_path: str | Path, ledger: sqlite3.Connection, *, account_id: str, dry_run: bool = False) -> ImportReport:
    v1 = open_v1_readonly(v1_path)
    try:
        rep = import_deals(v1, ledger, account_id=account_id, dry_run=dry_run)
        rep.snapshots = import_snapshots(v1, ledger, account_id=account_id, dry_run=dry_run)
        return rep
    finally:
        v1.close()

