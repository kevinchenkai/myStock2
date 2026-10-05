"""外汇视图（LN-06）：美元—人民币（USD/CNY）汇率与历史。

路径：直接币对、反向币对，或经 USD 中转（HKD→USD→CNY）；每一段都显示来源与汇率日期。缺任一段即「不可用」，
**不当作 1、不拿过旧的值冒充**（`max_stale_days` 之外视为缺失）。汇率是比率不是涨跌，用中性色。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mystock2.core.money import dec
from mystock2.market.fx import FxUnavailable
from mystock2.web import common as C
from mystock2.web.fxpath import describe_legs, resolve

CCYS = ("USD", "CNY")                 # 负责人只关注美元—人民币（2026-10-05）；HKD 的换算仍由账户总览/资产趋势经 fxpath 解析，不在本页展示
GAP_DAYS = 4


def run(conn, params):
    now = C.now_of(params)
    today = now.date()
    max_stale = params.get("max_stale_days", 4)
    ledger_ccys = {r["currency"] for r in conn.execute("SELECT DISTINCT currency FROM ledger_event")}

    paths = []
    for a in CCYS:
        for b in CCYS:
            if a == b:
                continue
            row = {"from": a, "to": b, "needed": a in ledger_ccys and b in ledger_ccys}
            try:
                r = resolve(conn, a, b, today, max_stale_days=max_stale)
                row.update(rate=C.rate_cell(r.rate), path="→".join(r.path), direct=len(r.path) == 2, rate_date=r.oldest_rate_date,
                           stale_days=r.max_stale_days, source=describe_legs(r), status="ok")
            except FxUnavailable as exc:
                row.update(rate=C.na_cell(str(exc)), path="—", direct=None, rate_date=None, stale_days=None, source="不可用", status="unavailable")
            paths.append(row)

    pair = params.get("pair", "USDCNY")
    rev = pair[3:] + pair[:3]
    since = (today - timedelta(days=params.get("days", 120))).isoformat()
    raw = conn.execute("SELECT pair, rate_date, version, source, rate, event_at, received_at FROM fx_rate WHERE pair IN (?,?) AND rate_date>=? "
                       "ORDER BY rate_date, received_at, version", (pair, rev, since)).fetchall()
    by_date: dict[str, dict] = {}
    for r in raw:                                       # 同一日取最后收到的一条（含反向币对）
        inv = r["pair"] != pair
        rate = (Decimal(1) / dec(r["rate"])) if inv else dec(r["rate"])
        by_date[r["rate_date"]] = {"date": r["rate_date"], "rate": rate, "source": r["source"], "inverse": inv, "received_at": r["received_at"],
                                   "event_at": r["event_at"]}
    history = [by_date[k] for k in sorted(by_date)]
    gaps = []
    for x, y in zip(history, history[1:], strict=False):
        d = (date.fromisoformat(y["date"]) - date.fromisoformat(x["date"])).days
        if d > GAP_DAYS:
            gaps.append({"from": x["date"], "to": y["date"], "days": d})

    latest = conn.execute("SELECT pair, MAX(rate_date) AS d, MAX(event_at) AS e, MAX(received_at) AS r FROM fx_rate WHERE pair IN ('USDCNY', 'CNYUSD') GROUP BY pair ORDER BY pair").fetchall()
    ev = max((r["e"] for r in latest), default=None)
    rc = max((r["r"] for r in latest), default=None)
    notes = ["汇率是比率，不是涨跌，不按红涨绿跌着色", f"换算最多接受比今天早 {max_stale} 天的汇率，超出即「不可用」"]
    if not latest:
        notes.append("库里没有任何汇率数据：所有换算显示「不可用」")
    return {
        "currencies": list(CCYS), "paths": paths, "pair": pair, "history_days": params.get("days", 120),
        "history": [{"date": h["date"], "rate": h["rate"], "source": h["source"], "inverse": h["inverse"], "received_at": h["received_at"]} for h in history],
        "gaps": gaps,
        "stored_pairs": [{"pair": r["pair"], "latest_date": r["d"], "received_at": r["r"]} for r in latest],
        "_freshness": C.freshness([C.source("汇率", ev, rc)], notes),
    }
