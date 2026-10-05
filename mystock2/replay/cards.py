"""逐笔复盘卡（RP-01、RP-04）。数据来自账本与行情，不是重撮合；事实与推测分开；无意图记录则写「动机未记录」。

执行质量（事后诊断）：买入 (成交价−当日最低)/(当日最高−当日最低)，越小越好；卖出取反；当日区间退化则不计算。
结果：成交后 1/5/20 个交易日的价格变化（复权口径）、持有期内最大不利/有利变动；平仓回合的费用后盈亏见 rounds。
**不定义「当时应成交的最优价」**；买卖后的最高/最低价只是事后诊断。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.core.timeutil import to_market_time
from mystock2.instruments.code_map import market_of
from mystock2.ledger.projection import effective_events
from mystock2.market.bars import get_daily

HORIZONS = (1, 5, 20)


@dataclass
class ReviewCard:
    deal_id: str
    code: str
    side: str
    fill_at: str
    local_date: date
    price: Decimal
    qty: Decimal
    fee: Decimal | None
    inventory_before: Decimal
    inventory_after: Decimal
    evidence: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def fills_and_fees(conn_ledger: sqlite3.Connection, account_id: str, opening_cutoff: str | None = None) -> tuple[list[dict], dict[str, Decimal]]:
    """从账本取有效 FILL（按时间）与按成交归属的实际费用（正数）。开账期初事件标 opening=True。"""
    events = effective_events(conn_ledger, account_id)
    fills, fees = [], {}
    for e in events:
        if e["event_type"] == "FILL":
            market = market_of(e["code"])
            fills.append({"code": e["code"], "date": to_market_time(e["event_at"], market).date(), "at": e["event_at"], "qty": dec(e["qty_delta"]),
                          "price": dec(e["price"]), "deal_id": e["ref_deal_id"] or e["business_key"], "currency": e["currency"], "opening": False})
        elif e["event_type"] == "OPENING_POSITION":
            fills.append({"code": e["code"], "date": to_market_time(e["event_at"], market_of(e["code"])).date(), "at": e["event_at"], "qty": dec(e["qty_delta"]),
                          "price": Decimal(0), "deal_id": e["business_key"], "currency": e["currency"], "opening": True})
        elif e["event_type"] in ("FEE", "TAX") and e["ref_deal_id"]:
            fees[e["ref_deal_id"]] = fees.get(e["ref_deal_id"], Decimal(0)) - dec(e["cash_delta"])
    opening_at = conn_ledger.execute("SELECT opening_at FROM account_opening WHERE account_id=?", (account_id,)).fetchone()
    if opening_at:                                            # 开账日之前的历史成交只作描述，不进入前向卡片/回合
        t0 = opening_at["opening_at"]
        fills = [f for f in fills if f["opening"] or f["at"] > t0]
    return sorted(fills, key=lambda f: (f["at"], f["deal_id"])), fees


def _outcome(conn_market, code: str, d: date, price: Decimal, side: str) -> dict:
    market = market_of(code)
    sessions = cal.session_days(market, d, min(cal.COVERAGE_END, d + timedelta(days=60)))
    rows = get_daily(conn_market, code, sessions[0], sessions[-1]) if sessions else []
    by = {r["session_date"]: r for r in rows}
    if d.isoformat() not in by or by[d.isoformat()]["adj_close"] is None:
        return {"status": "no_quotes"}
    f_d = dec(by[d.isoformat()]["adj_close"]) / dec(by[d.isoformat()]["close"])
    base = price * f_d
    out = {"status": "ok", "horizons": {}}
    future = [by[s.isoformat()] for s in sessions[1:] if s.isoformat() in by and by[s.isoformat()]["adj_close"] is not None]
    for n in HORIZONS:
        if len(future) >= n:
            r = future[n - 1]
            out["horizons"][n] = str((dec(r["adj_close"]) / base) - 1)
        else:
            out["horizons"][n] = None                         # 尚未到期：不记零
    window = future[:max(HORIZONS)]
    if window:
        def adj(r, k):
            return dec(r[k]) * dec(r["adj_close"]) / dec(r["close"])
        lows = min(adj(r, "low") for r in window)
        highs = max(adj(r, "high") for r in window)
        adverse, favorable = (lows / base - 1, highs / base - 1) if side == "BUY" else (1 - highs / base, 1 - lows / base)
        out["max_adverse"], out["max_favorable"] = str(adverse), str(favorable)
        out["window_days"] = len(window)
    return out


def build_cards(conn_ledger: sqlite3.Connection, conn_market: sqlite3.Connection, account_id: str, *, code: str | None = None) -> list[ReviewCard]:
    fills, fees = fills_and_fees(conn_ledger, account_id)
    inv: dict[str, Decimal] = {}
    cards: list[ReviewCard] = []
    for f in fills:
        before = inv.get(f["code"], Decimal(0))
        inv[f["code"]] = before + f["qty"]
        if f["opening"] or (code and f["code"] != code):
            continue
        side = "BUY" if f["qty"] > 0 else "SELL"
        c = ReviewCard(f["deal_id"], f["code"], side, f["at"], f["date"], f["price"], abs(f["qty"]), fees.get(f["deal_id"]), before, inv[f["code"]])
        c.facts.append(f"{f['date']} {side} {abs(f['qty'])} 股 @ {f['price']}（成交前库存 {before}，成交后 {inv[f['code']]}）")
        if c.fee is None:
            c.gaps.append("费用未记录（费用前口径）")
        # 当时证据（成交前已存在的）
        intents = conn_ledger.execute("SELECT action, limit_price, qty, recorded_at, seen_ai FROM intent WHERE code=? AND target_session=? AND recorded_at<=? ORDER BY recorded_at",
                                      (f["code"], f["date"].isoformat(), f["at"])).fetchall() if _has_table(conn_ledger, "intent") else []
        tickets = conn_ledger.execute("SELECT line_id, action, limit_price, qty, visible_at FROM ticket WHERE code=? AND target_session=? AND visible_at<=? AND status='frozen'",
                                      (f["code"], f["date"].isoformat(), f["at"])).fetchall() if _has_table(conn_ledger, "ticket") else []
        c.evidence = {"intents": [dict(r) for r in intents], "tickets_existing": [dict(r) for r in tickets]}
        if not intents:
            c.gaps.append("动机未记录")                                      # 不补写动机（RP-04）
        # 执行质量
        r = get_daily(conn_market, f["code"], f["date"], f["date"])
        if r:
            hi, lo = dec(r[0]["high"]), dec(r[0]["low"])
            if hi > lo:
                pos = (f["price"] - lo) / (hi - lo)
                c.execution = {"range_position": str(pos if side == "BUY" else 1 - pos), "note": "越小越好（买＝越接近当日最低；卖＝越接近当日最高）；事后诊断，不定义最优成交价"}
                c.inferences.append("执行质量只反映当日价格区间内的相对位置，不能说明当时是否本可以更好成交")
            else:
                c.execution = {"range_position": None, "note": "当日区间退化，不计算"}
        else:
            c.gaps.append("缺少当日行情，无法计算执行质量")
        c.outcome = _outcome(conn_market, f["code"], f["date"], f["price"], side)
        if c.outcome.get("status") == "no_quotes":
            c.gaps.append("缺少复权行情，无法计算成交后结果")
        cards.append(c)
    return cards


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def render_card_text(c: ReviewCard) -> str:
    """文字渲染：事实、证据、诊断（推测）、缺口分开标注。"""
    lines = [f"【事实】{x}" for x in c.facts]
    if c.fee is not None:
        lines.append(f"【事实】实际费用 {c.fee}")
    lines.append("【当时证据】" + (f"{len(c.evidence['intents'])} 条事前意图；" if c.evidence["intents"] else "无事前意图（动机未记录）；")
                 + (f"成交前已冻结的 AI 单 {len(c.evidence['tickets_existing'])} 张" if c.evidence["tickets_existing"] else "成交前没有已冻结的 AI 单"))
    if c.execution.get("range_position") is not None:
        lines.append(f"【诊断】执行质量（区间位置，越小越好）{float(Decimal(c.execution['range_position'])):.2f}")
    if c.outcome.get("status") == "ok":
        h = {k: (f"{float(Decimal(v)) * 100:.2f}%" if v is not None else "未到期") for k, v in c.outcome["horizons"].items()}
        lines.append(f"【结果】成交后价格变化（复权）：{h}")
    lines += [f"【推测】{x}" for x in c.inferences] + [f"【缺口】{x}" for x in c.gaps]
    return "\n".join(lines)



