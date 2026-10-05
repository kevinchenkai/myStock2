"""盈亏（已实现，移动平均成本法）。

- 「已实现交易盈亏」按币种分列，并拆成 **精确**（成本全来自开账后的买入）与 **估算**（平均成本混有开账快照成本）；
  没有成本证据的卖出部分「不可用」（只给数量与净收入，**不给盈亏**）。开账日（含）以前的成交不产生盈亏，单列。
- 「成交净现金流」是现金流水（买入为负、卖出为正），**不是盈亏**：单独命名、中性色。
- 本视图的盈亏不含股息、利息、汇兑；费用已计入（买入费用进成本、卖出费用冲减收入），另列费用合计。
"""
from __future__ import annotations

from decimal import Decimal

from mystock2.ledger.pnl import QUALITY_TEXT, compute_realized_pnl, trade_net_cashflow
from mystock2.web import common as C
from mystock2.web.ledgerdata import load_trades
from mystock2.web.rowcells import sell_pnl_cell

ZERO = Decimal(0)


def run(conn, params):
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    t = load_trades(conn, aid)
    code = (params.get("code") or "").strip()
    evs = [e for e in t.trade_events if not code or e.code == code]
    res = compute_realized_pnl(evs, t.opening_at)

    by_ccy = res.totals_by_currency()
    flows = trade_net_cashflow(evs)           # 含开账前成交：现金流水是事实，不是盈亏
    ccys = sorted(set(by_ccy) | set(flows) | {p.currency for p in res.pre_opening})
    summary = []
    for ccy in ccys:
        tot = by_ccy.get(ccy, {})
        exact, est = tot.get("realized_exact", ZERO), tot.get("realized_estimated", ZERO)
        unavail = tot.get("unavailable_qty", ZERO)
        has_est = any(s.quality == "estimated" for s in res.sells if s.currency == ccy)
        summary.append({
            "currency": ccy,
            "realized_exact": C.money_cell(exact, ccy, colored=True, sign=True),
            "realized_estimated": C.money_cell(est, ccy, colored=True, sign=True, tag="估算") if has_est else C.text_cell("无"),
            "unavailable_qty": C.qty_cell(unavail), "has_unavailable": unavail > 0,
            "fees_total": C.money_cell(tot.get("fees_total", ZERO), ccy),
            # 成交净现金流：现金流水，不是盈亏；中性色
            "trade_net_cashflow": C.money_cell(flows.get(ccy, ZERO), ccy, sign=True),
            "pre_opening": sum(1 for p in res.pre_opening if p.currency == ccy),
        })

    codes = []
    for c in sorted(res.by_code.values(), key=lambda c: c.code):
        codes.append({
            "code": c.code, "currency": c.currency,
            "realized_exact": C.money_cell(c.realized_exact, c.currency, colored=True, sign=True),
            "realized_estimated": (C.money_cell(c.realized_estimated, c.currency, colored=True, sign=True, tag="估算")
                                   if c.realized_estimated else C.text_cell("无")),
            "unavailable_qty": C.qty_cell(c.unavailable_qty),
            "unavailable_net_proceeds": (C.money_cell(c.unavailable_net_proceeds, c.currency, tag="净收入，非盈亏")
                                         if c.unavailable_qty > 0 else C.text_cell("-")),
            "fees_total": C.money_cell(c.fees_total, c.currency), "sells": c.sells, "buys": c.buys,
        })

    sells = []
    for s in sorted(res.sells, key=lambda s: s.at, reverse=True):
        label = QUALITY_TEXT[s.quality]
        pnl_cell = sell_pnl_cell(s)
        sells.append({"at": s.at, "code": s.code, "qty": C.qty_cell(s.qty), "price": C.price_cell(s.price, s.currency),
                      "avg_cost": C.price_cell(s.avg_cost, s.currency) if s.avg_cost is not None else C.na_cell("没有成本证据"),
                      "net_proceeds": C.money_cell(s.net_proceeds, s.currency), "realized": pnl_cell, "quality": s.quality,
                      "quality_text": label, "unavailable_qty": C.qty_cell(s.unavailable_qty), "note": s.note, "ref": s.ref,
                      "currency": s.currency})

    pre = [{"at": p.at, "code": p.code, "side": C.text_cell("买入" if p.side == "BUY" else "卖出"), "qty": C.qty_cell(p.qty),
            "price": C.price_cell(p.price, p.currency), "pnl": C.na_cell("开账前成交没有成本证据，不产生盈亏"), "ref": p.ref,
            "currency": p.currency} for p in sorted(res.pre_opening, key=lambda p: p.at, reverse=True)]

    notes = [
        "已实现盈亏＝移动平均成本法、费用后；不含股息、利息、汇兑",
        "「成交净现金流」是买入为负、卖出为正的现金流水，不是盈亏",
        "估算＝平均成本混有开账快照里的券商成本；不可用＝没有成本证据（不记零）",
    ]
    if any(w.startswith("oversold:") for w in res.warnings):
        notes.append("存在超出可追溯库存的卖出（账本口径不完整），超出部分盈亏不可用")
    return {
        "account_id": aid, "accounts": [a["account_id"] for a in accts], "opening_at": t.opening_at, "code_filter": code or None,
        "summary": summary, "by_code": codes, "sells": sells, "pre_opening": pre,
        "warnings": sorted(set(res.warnings) | set(t.warnings)),
        "_freshness": C.freshness([C.ledger_source(conn, aid)], notes),
    }
