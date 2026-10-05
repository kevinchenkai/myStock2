"""开账与券商快照（实施方案 §6 不变量 1、WP2.9）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from mystock2.core.db import atomic
from mystock2.core.money import to_db
from mystock2.core.timeutil import iso_utc, utc_now
from mystock2.instruments.code_map import currency_of, market_of
from mystock2.ledger.events import EventDraft, LedgerError, SourceDraft, post_event


def create_snapshot(conn: sqlite3.Connection, account_id: str, captured_at, source: str,
                    positions: dict[str, dict], cash: dict[str, dict], source_record_id: str | None = None) -> str:
    """写入券商快照（只追加、幂等）。

    positions: {code: {"qty": "100", "sellable_qty": "100", "cost_basis": "…(历史列＝券商 cost_price，摊薄成本)", "average_cost": "平均成本", "diluted_cost": "摊薄成本"}}
    cash: {ccy: {"cash": "1000", "available": "...", "frozen": "..."}}
    """
    payload = {"p": {k: {a: to_db(b) for a, b in v.items() if b is not None} for k, v in sorted(positions.items())},
               "c": {k: {a: to_db(b) for a, b in v.items() if b is not None} for k, v in sorted(cash.items())}}
    sid = hashlib.sha256(f"{account_id}|{iso_utc(captured_at)}|{source}|{json.dumps(payload, sort_keys=True)}".encode()).hexdigest()[:24]
    with atomic(conn):
        if conn.execute("SELECT 1 FROM account_snapshot WHERE snapshot_id=?", (sid,)).fetchone():
            return sid
        conn.execute("INSERT INTO account_snapshot(snapshot_id, account_id, captured_at, source, source_record_id) VALUES (?,?,?,?,?)",
                     (sid, account_id, iso_utc(captured_at), source, source_record_id))
        for code, v in positions.items():
            market = market_of(code)
            conn.execute("INSERT INTO snapshot_position(snapshot_id, market, code, qty, sellable_qty, cost_basis, average_cost, diluted_cost) VALUES (?,?,?,?,?,?,?,?)",
                         (sid, market, code, to_db(v["qty"]), to_db(v["sellable_qty"]) if v.get("sellable_qty") is not None else None,
                          to_db(v["cost_basis"]) if v.get("cost_basis") is not None else None,
                          to_db(v["average_cost"]) if v.get("average_cost") is not None else None,
                          to_db(v["diluted_cost"]) if v.get("diluted_cost") is not None else None))
        for ccy, v in cash.items():
            conn.execute("INSERT INTO snapshot_cash(snapshot_id, currency, cash, available, frozen) VALUES (?,?,?,?,?)",
                         (sid, ccy.upper(), to_db(v["cash"]), to_db(v["available"]) if v.get("available") is not None else None,
                          to_db(v["frozen"]) if v.get("frozen") is not None else None))
    return sid


def record_opening(conn: sqlite3.Connection, account_id: str, opening_at, positions: dict[str, str], cash: dict[str, str],
                   *, snapshot_id: str | None = None, source: SourceDraft | None = None) -> list[str]:
    """记录开账：期初事件反映 t0 时点状态，之后的和式从 t0 起。一个账户只能有一个开账点（相同内容重复调用为幂等）。"""
    t0 = iso_utc(opening_at)
    with atomic(conn):
        row = conn.execute("SELECT opening_at FROM account_opening WHERE account_id=?", (account_id,)).fetchone()
        if row and row["opening_at"] != t0:
            raise LedgerError(f"账户已有开账点 {row['opening_at']}，不得改动")
        if row:                                              # 开账包整包冻结：重复调用必须与已有内容完全一致
            have = {r["business_key"]: (r["event_type"], r["qty_delta"], r["cash_delta"]) for r in conn.execute(
                "SELECT business_key, event_type, qty_delta, cash_delta FROM ledger_event WHERE account_id=? AND event_type IN ('OPENING_POSITION','OPENING_CASH') AND event_version=1",
                (account_id,))}
            want = {f"opening:{account_id}:pos:{c}": ("OPENING_POSITION", to_db(q), "0") for c, q in positions.items()}
            want.update({f"opening:{account_id}:cash:{c.upper()}": ("OPENING_CASH", "0", to_db(a)) for c, a in cash.items()})
            if have != want:
                raise LedgerError("开账包已冻结且与本次内容不同；变更只能走显式的追加更正（correct_event）")
        ids = []
        for code, qty in sorted(positions.items()):
            ids.append(post_event(conn, EventDraft(f"opening:{account_id}:pos:{code}", account_id, "OPENING_POSITION", t0, currency_of(code),
                                                   market=market_of(code), code=code, qty_delta=to_db(qty)), source=source).event_id)
        for ccy, amt in sorted(cash.items()):
            ids.append(post_event(conn, EventDraft(f"opening:{account_id}:cash:{ccy.upper()}", account_id, "OPENING_CASH", t0, ccy.upper(),
                                                   cash_delta=to_db(amt)), source=source).event_id)
        if not row:
            conn.execute("INSERT INTO account_opening(account_id, opening_at, snapshot_id, created_at) VALUES (?,?,?,?)",
                         (account_id, t0, snapshot_id, iso_utc(utc_now())))
        return ids


def add_split(conn: sqlite3.Connection, code: str, effective_at, ratio_num: int, ratio_den: int, *, source: str | None = None) -> str:
    """登记拆股因子（1 股变 ratio_num/ratio_den 股）。幂等；拆股不是固定数量增量（不变量 2）。"""
    market_of(code)
    aid = f"split:{code}:{iso_utc(effective_at)}"
    with atomic(conn):
        row = conn.execute("SELECT ratio_num, ratio_den FROM corporate_action WHERE action_id=?", (aid,)).fetchone()
        if row:
            if (row["ratio_num"], row["ratio_den"]) != (ratio_num, ratio_den):
                raise LedgerError(f"{aid} 已存在但比例不同")
            return aid
        conn.execute("INSERT INTO corporate_action(action_id, kind, market, code, ratio_num, ratio_den, effective_at, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (aid, "SPLIT", market_of(code), code, ratio_num, ratio_den, iso_utc(effective_at), source, iso_utc(utc_now())))
    return aid
