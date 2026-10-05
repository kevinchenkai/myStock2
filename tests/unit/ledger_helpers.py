"""账本测试的合成数据辅助（合成测试值，不含任何真实账户信息）。"""
from __future__ import annotations

from decimal import Decimal

from mystock2.core import db as dbmod
from mystock2.ledger.events import EventDraft, SourceDraft, ensure_account, fee_key, fill_key, post_event

ACCT = "A1"
T0 = "2026-03-02T00:00:00.000000Z"        # 开账时点
D1 = "2026-03-03T15:00:00.000000Z"
D2 = "2026-03-04T15:00:00.000000Z"
D3 = "2026-03-05T15:00:00.000000Z"


def make_db(tmp_path):
    path = tmp_path / "t.db"
    dbmod.migrate(path)
    conn = dbmod.connect_writer(path, "ledger")
    ensure_account(conn, ACCT, "futu", "REAL", "USD")
    return conn


def buy(conn, deal_id, code, qty, price, at, *, source=None):
    notional = Decimal(qty) * Decimal(price)
    ccy = "HKD" if code.startswith("HK.") else "USD"
    return post_event(conn, EventDraft(fill_key(ACCT, deal_id), ACCT, "FILL", at, ccy, code=code, price=str(price),
                                       qty_delta=str(qty), cash_delta=str(-notional), ref_deal_id=deal_id), source=source)


def sell(conn, deal_id, code, qty, price, at, *, source=None):
    notional = Decimal(qty) * Decimal(price)
    ccy = "HKD" if code.startswith("HK.") else "USD"
    return post_event(conn, EventDraft(fill_key(ACCT, deal_id), ACCT, "FILL", at, ccy, code=code, price=str(price),
                                       qty_delta=str(-Decimal(qty)), cash_delta=str(notional), ref_deal_id=deal_id), source=source)


def fee(conn, deal_id, amount, at, kind="commission", ccy="USD"):
    return post_event(conn, EventDraft(fee_key(ACCT, deal_id, kind), ACCT, "FEE", at, ccy, cash_delta=str(-Decimal(amount)), ref_deal_id=deal_id))


def src(source, sid, **payload):
    return SourceDraft(source, sid, payload or {"id": sid})
