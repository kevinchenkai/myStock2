"""交易流水（LN 交易视图）。

只展示账本里**已入账的成交**（撤单/失败/未成交不入账）。每笔：币种、费用及归属（按费用类型分列）、**来源数**（同一成交经几条来源通道
到达并归并）、是否被更正（版本数）。「成交净现金流」＝成交现金变动 + 归属费用，是现金流水，**不是盈亏**，不着色。
开账日（含）以前的成交标「开账前」：只作描述，不参与持仓/盈亏和式。
"""
from __future__ import annotations

from decimal import Decimal

from mystock2.web import common as C
from mystock2.web.ledgerdata import load_trades
from mystock2.web.rowcells import fill_row

ZERO = Decimal(0)


def run(conn, params):
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    t = load_trades(conn, aid)
    code = (params.get("code") or "").strip()
    fills = [f for f in t.fills if not code or f["code"] == code]
    fills.sort(key=lambda f: (f["event_at"], f["business_key"]), reverse=True)
    total = len(fills)
    shown = fills[: params.get("limit", 200)]

    rows = [fill_row(f) for f in shown]

    # 逐币种：成交净现金流合计（只在同币种内相加）；只含已入账费用
    totals: dict[str, Decimal] = {}
    for f in fills:
        if f["fees"]:
            totals[f["currency"]] = totals.get(f["currency"], ZERO) + f["cash_delta"] - f["fee_total"]
    no_fee = sum(1 for f in fills if not f["fees"])
    srcs = [C.ledger_source(conn, aid)]
    notes = ["成交净现金流 = 成交现金变动 + 归属费用；它是现金流水，不是盈亏（买入未卖出只是现金变成持仓）"]
    if no_fee:
        notes.append(f"{no_fee} 笔成交没有费用事件入账，其净现金流显示「不可用」，合计不含这些笔")
    return {
        "account_id": aid, "accounts": [a["account_id"] for a in accts], "opening_at": t.opening_at, "code_filter": code or None,
        "total": total, "shown": len(rows), "rows": rows,
        "net_cashflow_totals": [{"currency": k, "amount": C.money_cell(v, k, sign=True)} for k, v in sorted(totals.items())],
        "unattributed_fees": [{"deal_id": u["deal_id"], "kind": u["kind"], "amount": C.money_cell(u["amount"], u["currency"])} for u in t.unattributed_fees],
        "warnings": list(t.warnings),
        "_freshness": C.freshness(srcs, notes),
    }
