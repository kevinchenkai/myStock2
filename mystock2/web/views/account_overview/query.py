"""账户总览（LN-03）：逐币种现金/持仓市值/应收/权益；基准币种合计须有汇率；对账状态与未对账项。

权益 E = 现金 + Σ(持仓数量 × **未复权**收盘价) + 应收（§6A.5）。缺行情的标的使该币种的市值与权益「不可用」
（不记零），并列出缺哪些标的；币种之间**不相加**——只有选了基准币种、且每个币种都有可用汇率时才给合计。
"""
from __future__ import annotations

from decimal import Decimal

from mystock2.core.money import dec
from mystock2.instruments.code_map import currency_of
from mystock2.ledger.projection import project
from mystock2.ledger.reconcile import reconcile
from mystock2.market.fx import FxUnavailable
from mystock2.web import common as C
from mystock2.web.fxpath import describe_legs, resolve
from mystock2.web.valuation import latest_close

ZERO = Decimal(0)


def _price_cell(px):
    if px.close is None:
        return C.na_cell(px.reason)
    tag = f"陈旧 {px.session_date}" if px.stale else None
    return C.price_cell(px.close, currency_of(px.code), tag=tag, title=f"收盘日 {px.session_date}（应有最近收盘日 {px.expected_session}）")


def _reconciliation(conn, aid, snap):
    if snap is None:
        return {"status": "no_snapshot", "label": "未对账（没有券商快照）", "snapshot": None, "items": []}
    rep = reconcile(conn, aid, snap["snapshot_id"])
    items = []
    for d in rep.position_diffs:
        items.append({"kind": "持仓", "text": f"{d['code']}：账本 {d['ledger']} ≠ 券商 {d['broker']}（差 {d['diff']}）"})
    for d in rep.cash_diffs:
        items.append({"kind": "现金", "text": f"{d['currency']}：账本 {C.fmt_money(d['ledger'], d['currency'])} ≠ 券商 {C.fmt_money(d['broker'], d['currency'])}"
                                              f"（差 {C.fmt_money(d['diff'], d['currency'])}，阈值 {d['tolerance']}）"})
    if rep.open_pending:
        items.append({"kind": "待匹配", "text": f"{rep.open_pending} 条来源记录身份不足，未入账（待匹配队列）"})
    for g in rep.incomplete_fx_groups:
        items.append({"kind": "换汇", "text": f"换汇组 {g} 缺腿，整组不生效"})
    return {"status": "ok" if rep.ok else "mismatch", "label": "已对账：与券商快照一致" if rep.ok else f"有 {len(items)} 项未对账",
            "snapshot": {"id": snap["snapshot_id"], "captured_at": snap["captured_at"], "source": snap["source"]}, "items": items,
            "warnings": rep.warnings}


def run(conn, params):
    now = C.now_of(params)
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    proj = project(conn, aid, as_of=now)
    snap = C.latest_snapshot(conn, aid)
    prices = {code: latest_close(conn, code, now) for code in sorted(proj.positions)}

    ccys = sorted(set(proj.cash) | set(proj.receivable) | {currency_of(c) for c in proj.positions})
    snap_cash = {}
    if snap is not None:
        snap_cash = {r["currency"]: dec(r["cash"]) for r in conn.execute("SELECT currency, cash FROM snapshot_cash WHERE snapshot_id=?", (snap["snapshot_id"],))}

    rows, positions = [], []
    raw = {}
    for ccy in ccys:
        held = {c: q for c, q in proj.positions.items() if currency_of(c) == ccy}
        missing = [c for c in held if prices[c].close is None]
        cash, recv = proj.cash.get(ccy, ZERO), proj.receivable.get(ccy, ZERO)
        valued = sum((q * prices[c].close for c, q in held.items() if prices[c].close is not None), ZERO)
        mv = None if missing else valued
        equity = None if missing else cash + valued + recv
        reason = f"缺行情：{', '.join(missing)}" if missing else None
        raw[ccy] = {"cash": cash, "market_value": mv, "equity": equity}
        for c, q in sorted(held.items()):
            px = prices[c]
            positions.append({"code": c, "currency": ccy, "qty": C.qty_cell(q), "price": _price_cell(px),
                              "market_value": C.money_cell(q * px.close, ccy) if px.close is not None else C.na_cell(px.reason)})
        rows.append({
            "currency": ccy, "cash": C.money_cell(cash, ccy),
            "receivable": C.money_cell(recv, ccy) if recv else None,
            "market_value": C.money_cell(mv, ccy, reason=reason),
            "market_value_partial": C.money_cell(valued, ccy, tag="仅已估值部分") if missing else None,
            "equity": C.money_cell(equity, ccy, reason=reason),
            "broker_cash": C.money_cell(snap_cash[ccy], ccy) if ccy in snap_cash else C.na_cell("快照中没有该币种现金"),
            "positions": len(held), "unvalued": missing,
            "stale_prices": sorted(c for c in held if prices[c].stale),
        })

    base = (params.get("base_ccy") or "").upper() or None
    total, rates, fx_sources = None, [], []
    if base:
        max_stale = params.get("fx_max_stale_days", 4)
        total = {"currency": base, "cash": None, "market_value": None, "equity": None, "unavailable": []}
        sums = {"cash": ZERO, "market_value": ZERO, "equity": ZERO}
        ok = {"cash": True, "market_value": True, "equity": True}
        for row in rows:
            ccy = row["currency"]
            try:
                res = resolve(conn, ccy, base, now.date(), max_stale_days=max_stale)
            except FxUnavailable as exc:
                total["unavailable"].append({"currency": ccy, "reason": str(exc)})
                row["converted"] = None
                rates.append({"from": ccy, "to": base, "rate": C.na_cell(str(exc)), "path": f"{ccy}→{base}", "source": "不可用"})
                ok = {k: False for k in ok}
                continue
            rates.append({"from": ccy, "to": base, "rate": C.rate_cell(res.rate), "path": "→".join(res.path), "source": describe_legs(res),
                          "rate_date": res.oldest_rate_date, "stale_days": res.max_stale_days,
                          "legs": [{"pair": leg["pair"], "inverse": leg["inverse"], "rate_date": leg["rate_date"], "source": leg["source"],
                                    "received_at": leg["received_at"]} for leg in res.legs]})
            for leg in res.legs:
                fx_sources.append(C.source(f"汇率 {leg['pair']}", leg["event_at"], leg["received_at"]))
            conv = {}
            for k in ("cash", "market_value", "equity"):
                v = raw[ccy][k]
                if v is None:
                    ok[k] = False
                    conv[k] = C.na_cell(f"{ccy} 该项不可用")
                else:
                    conv[k] = C.money_cell(v * res.rate, base)
                    sums[k] += v * res.rate
            row["converted"] = conv
        for k in sums:
            total[k] = C.money_cell(sums[k] if ok[k] and rows else None, base, reason="有币种缺汇率或缺行情，不合计" if not ok[k] else None)
        if not rows:
            total.update(cash=C.na_cell("没有任何币种数据"), market_value=C.na_cell(), equity=C.na_cell())

    warn = [f"{w}" for w in proj.warnings]
    if proj.pre_opening_events:
        warn.append(f"pre_opening:{proj.pre_opening_events}")

    srcs = [C.ledger_source(conn, aid), C.snapshot_source(snap)]
    if prices:
        srcs.append(C.source("行情（未复权收盘）", min((p.event_at for p in prices.values()), default=None) if all(p.event_at for p in prices.values()) else None,
                             min((p.received_at for p in prices.values()), default=None) if all(p.received_at for p in prices.values()) else None))
    if fx_sources:
        srcs.append(C.source("汇率", min(s["event_at"] or "" for s in fx_sources) or None, min(s["collected_at"] or "" for s in fx_sources) or None))
    elif base and rows:
        srcs.append(C.source("汇率", None, None))
    return {
        "account_id": aid, "accounts": [a["account_id"] for a in accts], "opening_at": proj.opening_at, "as_of": C.iso_utc(now),
        "base_ccy": base, "currencies": rows, "positions": positions, "total": total, "rates": rates,
        "reconciliation": _reconciliation(conn, aid, snap), "warnings": warn,
        "_freshness": C.freshness(srcs, ["权益使用未复权收盘价（复权价只用于特征）", "币种之间不相加；选基准币种才合计"]),
    }
