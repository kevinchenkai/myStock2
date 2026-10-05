"""资产趋势（LN-04、T-07）。按币种分别给三条曲线，币种之间不相加，**三者是不同的量，不得混用名称**：

1. 持仓市值 ＝ Σ 持仓数量 × 未复权收盘价；
2. 账户权益 ＝ 现金 + 持仓市值 + 应收；
3. 剔除外部资金流的收益 ＝ 自基准日起 (权益变化) − (外部资金流) − (换汇划转)；入金当天权益上升，收益不变。

某日缺任一持仓的收盘价 → 该日是「缺口」（status=gap，列出缺哪些标的），既不插值也不记零，前端断开折线。
"""
from __future__ import annotations

from datetime import timedelta

from mystock2.core import calendars as cal
from mystock2.core.timeutil import ensure_utc
from mystock2.instruments.code_map import currency_of
from mystock2.ledger.projection import load_events
from mystock2.web import common as C
from mystock2.web.series import build_equity_series, replay_states
from mystock2.web.valuation import closes_by_date, expected_session, latest_close

NAMES = {"market_value": "持仓市值", "equity": "账户权益", "profit": "剔除外部资金流的收益"}
HOME_MARKET = {"HKD": "HK", "USD": "US"}


def _axis(ccy, start, now):
    """该币种的日期轴：本币市场的交易日（无本币市场则取 HK/US 并集）。返回 [(date, 截止时点 UTC)]。"""
    markets = [HOME_MARKET[ccy]] if ccy in HOME_MARKET else ["HK", "US"]
    ends = [expected_session(m, now) for m in markets]
    ends = [e for e in ends if e is not None]
    if not ends:
        return []
    end = max(ends)
    cuts: dict = {}
    for m in markets:
        for d in cal.session_days(m, start, end):
            c = cal.session(m, d).close_utc
            cuts[d] = max(cuts.get(d, c), c)
    return sorted(cuts.items())


def _runs(points):
    """连续缺口段。"""
    out, cur = [], None
    for p in points:
        if p["status"] == "gap":
            if cur is None:
                cur = {"from": p["date"], "to": p["date"], "missing": set(p["missing"]), "days": 1}
            else:
                cur["to"] = p["date"]
                cur["days"] += 1
                cur["missing"] |= set(p["missing"])
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [{**r, "missing": sorted(r["missing"])} for r in out]


def run(conn, params):
    now = C.now_of(params)
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    rows = load_events(conn, aid)
    if not rows:
        raise C.ViewUnavailable("no_events", "账本里还没有事件，无法画趋势")
    op = conn.execute("SELECT opening_at FROM account_opening WHERE account_id=?", (aid,)).fetchone()
    opening_at = op["opening_at"] if op else None
    start_dt = ensure_utc(opening_at) if opening_at else min(ensure_utc(r["event_at"]) for r in rows)
    start = start_dt.date()
    codes = sorted({r["code"] for r in rows if r["code"] and r["event_type"] in ("FILL", "OPENING_POSITION")})
    code_ccy = {c: currency_of(c) for c in codes}
    splits = [dict(r) for r in conn.execute("SELECT code, effective_at, ratio_num, ratio_den FROM corporate_action WHERE kind='SPLIT'")]
    ccys = sorted({r["currency"] for r in rows} | set(code_ccy.values()))
    max_points = params.get("max_points", 250)

    end_all = max([e for e in (expected_session(m, now) for m in ("HK", "US")) if e] or [start])
    prices = {c: closes_by_date(conn, c, start - timedelta(days=1), end_all) for c in codes}

    series, notes, adjust_other = [], [], 0
    for ccy in ccys:
        axis = [(d, c) for d, c in _axis(ccy, start, now) if c >= start_dt]
        if not axis:
            continue
        states = replay_states(rows, splits, opening_at, [c for _, c in axis])
        pts, base_date = build_equity_series(ccy, [d for d, _ in axis], states, code_ccy, prices)
        adjust_other = max(adjust_other, states[-1].adjust_other)
        gaps = _runs(pts)
        shown = pts[-max_points:]
        last = next((p for p in reversed(pts) if p["status"] == "ok"), None)
        series.append({
            "currency": ccy, "names": NAMES, "base_date": base_date, "points": shown, "gaps": [g for g in gaps if g["to"] >= shown[0]["date"]],
            "total_points": len(pts), "shown_points": len(shown),
            "flows": [{"date": p["date"], "amount": C.money_cell(p["flow"], ccy, sign=True)} for p in shown if p["flow"] != 0],
            "latest": None if last is None else {
                "date": last["date"],
                "market_value": C.money_cell(last["market_value"], ccy),
                "equity": C.money_cell(last["equity"], ccy),
                "profit": C.money_cell(last["profit"], ccy, colored=True, sign=True),
            },
            "latest_is_gap": bool(pts and pts[-1]["status"] == "gap"),
        })
    if not series:
        raise C.ViewUnavailable("no_axis", "没有可用的交易日轴（开账日之后还没有已收盘的交易日）")
    if adjust_other:
        notes.append(f"含 {adjust_other} 笔非外部资金流的 ADJUST：按账本口径计入权益变化，其性质请在账本中核对")
    if not opening_at:
        notes.append("未登记开账点：以首个事件日为起点，账本和式可能不完整")
    snaps = [r["captured_at"] for r in conn.execute("SELECT captured_at FROM account_snapshot WHERE account_id=? ORDER BY captured_at", (aid,))]

    px = [latest_close(conn, c, now) for c in codes]
    quote_src = C.source("行情（未复权收盘）", min(p.event_at for p in px) if px and all(p.event_at for p in px) else None,
                         min(p.received_at for p in px) if px and all(p.received_at for p in px) else None)
    srcs = [C.ledger_source(conn, aid)] + ([quote_src] if codes else [])
    notes += ["三条曲线是不同的量：持仓市值 ≠ 账户权益 ≠ 剔除外部资金流的收益",
              "缺行情的日子标缺口、不连线；入金/出金当天只改变权益，不改变「剔除外部资金流的收益」",
              "单币种曲线中，换汇划转也不计为该币种收益"]
    return {"account_id": aid, "accounts": [a["account_id"] for a in accts], "opening_at": opening_at, "names": NAMES, "series": series,
            "broker_snapshots": snaps[-50:], "snapshot_count": len(snaps),
            "_freshness": C.freshness(srcs, notes)}
