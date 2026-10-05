"""复盘视图（M7；RP-01…RP-04）。

数据来自账本与行情，**不是重撮合**；调用 `replay.cards/rounds/behavior` 的纯逻辑（口径单一来源）。
- 复盘卡：事实 / 当时证据 / 诊断（推测）/ 结果 / 缺口 分栏；没有事前意图则写「动机未记录」，不补写动机。
- 行为指标：含样本量与口径；样本不足显示「不足」，不显示 0。
- 诊断回合（FIFO）：诊断口径，**不是账本收益**；与盈亏视图（移动平均成本）口径不同，分区呈现、不相加。
- 密封：卡片里「成交前已冻结的 AI 单」只有在该（批次、市场、目标日）已揭示时才显示内容，否则只写「已密封」。
"""
from __future__ import annotations

import json

from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc
from mystock2.instruments.code_map import CodeError, currency_of
from mystock2.replay.behavior import MIN_SAMPLE, behavior_metrics
from mystock2.replay.cards import build_cards, fills_and_fees
from mystock2.replay.rounds import build_rounds
from mystock2.web import common as C
from mystock2.web import sealing

SIDE_TEXT = {"BUY": "买入", "SELL": "卖出"}
ACTION_TEXT = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有", "SKIP": "不操作", "NO_TRADE": "不交易"}
FLAG_TEXT = {"cost_unknown": "成本未知（期初库存无成本证据，不给盈亏）", "fees_missing": "费用缺失（费用前口径）",
             "sold_without_inventory": "卖出时账本无库存（数据缺口）"}
INSUFFICIENT = "不足"


def _ccy(code: str) -> str | None:
    try:
        return currency_of(code)
    except CodeError:
        return None


def _ai_evidence(conn, kinds, code: str, target: str, fill_at: str, now) -> list[dict]:
    """成交前已冻结的 AI 单：已揭示才给内容，否则只写已密封（不含动作/限价/数量/原因）。"""
    fill_dt = ensure_utc(fill_at)
    rows = [r for r in conn.execute("SELECT * FROM ticket WHERE code=? AND target_session=? AND kind='line_sim' AND status='frozen' ORDER BY visible_at, rowid",
                                    (code, target)) if sealing.is_ai_line(r["line_id"], kinds) and ensure_utc(r["visible_at"]) <= fill_dt]
    cells: dict[tuple[str, str, str], list] = {}
    for r in rows:
        cells.setdefault((r["batch_id"], r["market"], r["line_id"]), []).append(r)
    out = []
    for (batch_id, market, line_id), vs in sorted(cells.items()):
        revealed = sealing.reveal_info(conn, batch_id, market, target, now) is not None
        kind = kinds.get(line_id) or line_id.rsplit(":", 1)[-1]
        if not revealed:
            out.append({"line_kind": kind, "state": "sealed", "text": f"成交前已有 {len(vs)} 个已冻结版本的 AI 单（{kind}）：已密封，未揭示，不显示内容"})
            continue
        v = vs[-1]
        ccy = _ccy(code)
        out.append({"line_kind": kind, "state": "revealed", "visible_at": v["visible_at"], "action": ACTION_TEXT.get(v["action"], v["action"]),
                    "limit_price": C.price_cell(v["limit_price"], ccy) if v["limit_price"] is not None else C.na_cell("该单没有限价"),
                    "qty": C.qty_cell(v["qty"]) if v["qty"] is not None else C.na_cell("该单没有数量"), "reasons": json.loads(v["reason_json"]),
                    "text": f"成交前已冻结的 AI 单（{kind}，已揭示）：{ACTION_TEXT.get(v['action'], v['action'])}"})
    return out


def _intent_text(i: dict, ccy: str | None) -> str:
    base = ACTION_TEXT.get(i["action"], i["action"])
    if i.get("limit_price") is not None and i.get("qty") is not None:
        base += f" {C.fmt_qty(i['qty'])} 股 @ {C.fmt_price(i['limit_price'])}" + (f" {ccy}" if ccy else "")
    return f"{base}（记录于 {i['recorded_at']}；{'看过 AI 之后记录' if i['seen_ai'] else '未看 AI 之前记录'}）"


def _card(c, conn, kinds, now) -> dict:
    ccy = _ccy(c.code)
    horizons = []
    oc = c.outcome or {}
    for n in (1, 5, 20):
        v = (oc.get("horizons") or {}).get(n) if oc.get("status") == "ok" else None
        change = C.pct_cell(v, colored=True) if v is not None else \
            C.na_cell("尚未到期或缺行情", label="未到期" if oc.get("status") == "ok" else C.UNAVAILABLE)
        horizons.append({"days": n, "change": change})
    rp = (c.execution or {}).get("range_position")
    intents = [dict(i) for i in c.evidence.get("intents", [])]
    ai = _ai_evidence(conn, kinds, c.code, c.local_date.isoformat(), c.fill_at, now)
    evidence = [_intent_text(i, ccy) for i in intents] or ["无事前意图（动机未记录）"]
    evidence += [a["text"] for a in ai] or ["成交前没有已冻结的 AI 单"]
    diagnosis = []
    if rp is not None:
        diagnosis.append("执行质量（区间位置，越小越好）：" + C.fmt_decimal(rp, 2) + "；事后诊断，不定义最优成交价")
    elif c.execution:
        diagnosis.append(c.execution.get("note", "不计算"))
    diagnosis += list(c.inferences)
    facts = list(c.facts)
    if c.fee is not None:
        facts.append("实际费用 " + C.fmt_money(c.fee, ccy or ""))
    return {
        "deal_id": c.deal_id, "code": c.code, "side": c.side, "side_text": SIDE_TEXT.get(c.side, c.side), "date": c.local_date.isoformat(), "fill_at": c.fill_at,
        "price": C.price_cell(c.price, ccy), "qty": C.qty_cell(c.qty),
        "fee": C.money_cell(c.fee, ccy, reason="费用未记录（费用前口径）") if c.fee is not None and ccy else C.na_cell("费用未记录（费用前口径）"),
        "motive": C.text_cell("动机未记录" if not intents else f"事前意图 {len(intents)} 条", tag=None if intents else "缺口"),
        "range_position": C.na_cell("缺行情或当日区间退化") if rp is None else {"text": C.fmt_decimal(rp, 2), "v": str(rp)},
        "outcomes": horizons,
        "detail": {
            "facts": facts, "evidence": evidence, "diagnosis": diagnosis, "gaps": list(c.gaps), "ai_tickets": ai,
            "outcome": {
                "horizons": horizons,
                "max_adverse": C.pct_cell(oc.get("max_adverse"), colored=True, reason="没有成交后行情") if oc.get("max_adverse") is not None else C.na_cell("没有成交后行情"),
                "max_favorable": C.pct_cell(oc.get("max_favorable"), colored=True, reason="没有成交后行情") if oc.get("max_favorable") is not None else C.na_cell("没有成交后行情"),
                "window_days": oc.get("window_days"), "status": oc.get("status", "no_quotes"),
            },
            "inventory": {"before": C.qty_cell(c.inventory_before), "after": C.qty_cell(c.inventory_after)},
        },
    }


def _fmt_metric(name: str, value: str) -> str:
    if "比例" in name or "胜率" in name:
        return C.fmt_pct(dec(value), 1)
    if "笔数" in name:
        return C.fmt_decimal(dec(value), 0)
    if "天数" in name:
        return C.fmt_decimal(dec(value), 1) + " 天"
    return C.fmt_decimal(dec(value), 2)


def _metric_row(m) -> dict:
    insufficient = m.value is None
    return {"name": m.name, "n": m.n, "min_sample": MIN_SAMPLE, "definition": m.definition, "insufficient": insufficient,
            "value": C.na_cell(f"样本 {m.n} < {MIN_SAMPLE}", label=INSUFFICIENT) if insufficient else C.text_cell(_fmt_metric(m.name, m.value)),
            "sample": f"n={m.n}"}


def _round_row(r) -> dict:
    ccy = _ccy(r.code)
    pnl = C.money_cell(r.pnl_after_fees, ccy, colored=True, sign=True) if r.pnl_after_fees is not None and ccy else \
        C.na_cell("；".join(FLAG_TEXT.get(f, f) for f in r.flags) or "不可用")
    return {
        "code": r.code, "currency": ccy, "open_date": r.open_date.isoformat(), "close_date": r.close_date.isoformat(), "qty": C.qty_cell(r.qty),
        "cost_unit": C.price_cell(r.cost_unit, ccy) if r.cost_unit is not None else C.na_cell("期初库存无成本证据"),
        "exit_unit": C.price_cell(r.exit_unit, ccy), "fees": C.money_cell(r.fees, ccy) if ccy else C.na_cell(), "pnl": pnl, "holding_days": r.holding_days,
        "flags": [FLAG_TEXT.get(f, f) for f in r.flags], "tag": "诊断回合",
    }


def run(conn, params):
    now = C.now_of(params)
    acct, accts = C.resolve_account(conn, params)
    aid = acct["account_id"]
    code = (params.get("code") or "").strip() or None
    kinds = sealing.line_kinds(conn)
    cards = build_cards(conn, conn, aid, code=code)
    fills, fees = fills_and_fees(conn, aid)
    if code:
        fills = [f for f in fills if f["code"] == code]
    rounds, _ = build_rounds(fills, fees)
    metrics = behavior_metrics(cards, rounds)

    limit = params.get("limit", 100)
    shown = sorted(cards, key=lambda c: (c.fill_at, c.deal_id), reverse=True)[:limit]
    card_rows = [_card(c, conn, kinds, now) for c in shown]
    srcs = [C.ledger_source(conn, aid)]
    codes = sorted({c.code for c in cards})
    if codes:
        ev, rc = [], []
        for cd in codes:
            r = conn.execute("SELECT MAX(event_at) AS e, MAX(received_at) AS r FROM quote_daily WHERE code=?", (cd,)).fetchone()
            ev.append(r["e"])
            rc.append(r["r"])
        srcs.append(C.source("行情（日线）", min(ev) if all(ev) else None, min(rc) if all(rc) else None))
    return {
        "account_id": aid, "accounts": [a["account_id"] for a in accts], "code_filter": code,
        "cards": {"title": "逐笔复盘卡", "total": len(cards), "shown": len(card_rows), "rows": card_rows,
                  "note": "数据来自账本与行情，不是重撮合；事实与推测分开；无事前意图则写「动机未记录」。「对照」（AI 单与不操作反事实）未实现。"},
        "behavior": {"title": "行为指标（描述性）", "min_sample": MIN_SAMPLE, "rows": [_metric_row(m) for m in metrics],
                     "note": f"样本 < {MIN_SAMPLE} 显示「{INSUFFICIENT}」，不显示 0，不下确定性结论。"},
        "rounds": {"title": "诊断回合（FIFO）", "rows": [_round_row(r) for r in rounds], "tag": "诊断回合",
                   "note": "诊断回合按先进先出配对真实成交，用于行为诊断；它不是账本收益口径（盈亏视图用移动平均成本法），两者不可相加或直接比较。"},
        "_freshness": C.freshness(srcs, ["复盘卡与回合是诊断口径，与账本/记分牌的收益口径不同，分区呈现"]),
    }
