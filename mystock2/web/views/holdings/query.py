"""我的持仓（LN-05）。

三类成本**并列且互不覆盖**：
- 券商成本：来自最近一份券商快照的 `cost_basis`（可空；**此处按每股成本展示——字段口径待 M2a 采集器确认**）；
- 摊薄成本：快照没有该字段 → 「不可用」（不拿别的成本冒充）；
- 本地移动平均成本：由账本成交按移动平均法算出（`ledger.pnl`）；含开账估算成本时标「估算」，有无成本证据的股份时标「部分」。
角色（核心/交易/观察）来自标的名单配置（可选，缺失显示「未配置」）；「当前操作单」只显示该标的在最近目标日是否已有已冻结的 AI 单——
**只有「已密封/已揭示」状态，未揭示时绝不显示动作**（密封见 `web/sealing.py`；内容请到「操作单」视图）。
"""
from __future__ import annotations

from decimal import Decimal

from mystock2.core.money import dec
from mystock2.instruments.code_map import currency_of
from mystock2.ledger.pnl import compute_realized_pnl
from mystock2.ledger.projection import project
from mystock2.web import common as C
from mystock2.web import sealing
from mystock2.web.ledgerdata import load_trades
from mystock2.web.valuation import latest_close

ZERO = Decimal(0)
ROLE_TEXT = {"core": "核心", "trade": "交易", "watch": "观察"}


def _roles(path):
    """标的名单 → {code: tier}；缺失/无效不报错，只返回说明。"""
    if not path:
        return {}, "未配置标的名单（config/local/universe.yaml），角色显示「未配置」"
    try:
        from mystock2.instruments.universe import load_universe
        rep = load_universe(path)
    except Exception as exc:                           # noqa: BLE001 — 角色是可选信息，失败不得影响持仓展示
        return {}, f"标的名单无法读取：{type(exc).__name__}"
    roles = {e.code: e.tier for e in rep.entries}
    return roles, ("" if rep.ok else "标的名单有校验错误，角色可能不完整")


def _order_cell(st):
    """当前操作单：只给状态，不给任何内容（动作/限价/数量）。"""
    if st is None:
        return C.text_cell("无已冻结的 AI 单")
    if st["state"] == "revealed":
        return C.text_cell(f"已揭示（目标日 {st['target']}）", title="内容请到「操作单」视图查看")
    return C.text_cell(f"已密封（目标日 {st['target']}）", title="揭示前不显示动作；揭示只能经命令行写入暴露日志")


def run(conn, params):
    now = C.now_of(params)
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    proj = project(conn, aid, as_of=now)
    snap = C.latest_snapshot(conn, aid)
    spos = {}
    if snap is not None:
        spos = {r["code"]: r for r in conn.execute("SELECT * FROM snapshot_position WHERE snapshot_id=?", (snap["snapshot_id"],))}
    trades = load_trades(conn, aid)
    pnl = compute_realized_pnl(trades.trade_events, trades.opening_at)
    roles, role_note = _roles(params.get("_universe_path"))
    prices = {c: latest_close(conn, c, now) for c in sorted(set(proj.positions) | set(spos))}
    ai_state = sealing.latest_ai_status_by_code(conn, now)

    # 每个币种的已估值市值合计，用于集中度（占该币种持仓市值，币种之间不相加）
    mv_by_ccy: dict[str, Decimal] = {}
    missing_by_ccy: dict[str, bool] = {}
    for c, q in proj.positions.items():
        ccy = currency_of(c)
        if prices[c].close is None:
            missing_by_ccy[ccy] = True
        else:
            mv_by_ccy[ccy] = mv_by_ccy.get(ccy, ZERO) + q * prices[c].close

    rows = []
    for code in sorted(set(proj.positions) | {c for c, r in spos.items() if dec(r["qty"]) != 0}):
        ccy = currency_of(code)
        qty = proj.positions.get(code, ZERO)
        px = prices[code]
        sp = spos.get(code)
        cp = pnl.by_code.get(code)
        mv = qty * px.close if px.close is not None else None
        # 券商成本（快照原值）
        broker_cost = C.price_cell(dec(sp["average_cost"]), ccy, tag="券商平均成本") if sp is not None and sp["average_cost"] is not None else \
            C.na_cell("最近快照没有该标的的平均成本" if sp is not None else "没有快照")
        diluted = C.price_cell(dec(sp["diluted_cost"]), ccy, tag="券商摊薄成本", title="摊薄成本把已实现盈亏摊入持仓，可为负；它不是持仓的平均成本") \
            if sp is not None and sp["diluted_cost"] is not None else C.na_cell("最近快照没有摊薄成本")
        # 本地移动平均成本
        if cp is None or cp.avg_cost is None:
            local = C.na_cell("没有可追溯的成本证据（开账持仓无成本，且之后无买入）" if cp is not None or qty else "无持仓")
            unreal = C.na_cell("本地成本不可用")
        else:
            tag = "估算" if cp.cost_estimated else None
            if cp.unknown_qty > 0:
                tag = "部分" if tag is None else tag + "·部分"
            title = []
            if cp.cost_estimated:
                title.append("平均成本混有开账快照成本（估算）")
            if cp.unknown_qty > 0:
                title.append(f"另有 {C.fmt_qty(cp.unknown_qty)} 股没有成本证据，不在此平均成本内")
            local = C.price_cell(cp.avg_cost, ccy, tag=tag, title="；".join(title) or None)
            if px.close is None:
                unreal = C.na_cell("缺行情")
            else:
                u = (px.close - cp.avg_cost) * cp.known_qty
                unreal = C.money_cell(u, ccy, colored=True, sign=True, tag=tag, title="仅含有成本证据的股份" if cp.unknown_qty > 0 else None)
        weight = None
        if mv is not None and not missing_by_ccy.get(ccy) and mv_by_ccy.get(ccy):
            weight = C.pct_cell(mv / mv_by_ccy[ccy])
        sq = dec(sp["qty"]) if sp is not None else None
        rows.append({
            "code": code, "currency": ccy,
            "role": C.text_cell(ROLE_TEXT.get(roles.get(code), "未配置") if roles else "未配置"),
            "qty": C.qty_cell(qty),
            "broker_qty": C.qty_cell(sq) if sq is not None else C.na_cell("没有快照"),
            "qty_match": None if sq is None else sq == qty,
            "price": C.price_cell(px.close, ccy, tag=f"陈旧 {px.session_date}" if px.stale else None, title=f"收盘日 {px.session_date}") if px.close is not None else C.na_cell(px.reason),
            "market_value": C.money_cell(mv, ccy) if mv is not None else C.na_cell(px.reason),
            "weight": weight if weight is not None else C.na_cell("缺行情或无市值，无法算集中度"),
            "broker_cost": broker_cost, "diluted_cost": diluted, "local_cost": local, "unrealized": unreal,
            "order": _order_cell(ai_state.get(code)),
        })

    warn = list(proj.warnings) + list(trades.warnings)
    srcs = [C.ledger_source(conn, aid), C.snapshot_source(snap)]
    if prices:
        ev = [p.event_at for p in prices.values()]
        rc = [p.received_at for p in prices.values()]
        srcs.append(C.source("行情（未复权收盘）", min(ev) if all(ev) else None, min(rc) if all(rc) else None))
    notes = ["三类成本并列、互不覆盖；券商成本按每股展示（快照 cost_basis 口径待采集器确认）"]
    if role_note:
        notes.append(role_note)
    return {
        "account_id": aid, "accounts": [a["account_id"] for a in accts], "as_of": C.iso_utc(now), "rows": rows, "warnings": warn,
        "snapshot": {"id": snap["snapshot_id"], "captured_at": snap["captured_at"]} if snap is not None else None,
        "_freshness": C.freshness(srcs, notes),
    }
