"""否决精度（IN-02、实施方案 §4.2）：被否决/缩小的买单中，事后若成交本会亏损的比例。

反事实**只是模拟**：把被否决的数量按 exec-v1 的保守撮合在目标日重放，成交的部分与「目标日起 h 个交易日后」的未复权收盘价比较（扣同一费用档案）。
样本不足（< 5）时不给比例（显示「不足」）。这是对否决层的事后评估，不是对未来的承诺。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec
from mystock2.ledger.fees import FeeRule, estimate, select_rule
from mystock2.scoreboard.engine import MarketData
from mystock2.scoreboard.matcher import match_order
from mystock2.scoreboard.types import BUY, ExecProtocol, SimOrder

MIN_SAMPLE = 5


@dataclass
class VetoPrecision:
    vetoed_buys: int
    would_fill: int
    would_lose: int
    precision: Decimal | None
    horizon: int
    note: str


def veto_precision(conn: sqlite3.Connection, md: MarketData, *, market: str, fee_rules: list[FeeRule], protocol: ExecProtocol, horizon: int = 5) -> VetoPrecision:
    vetoed = would_fill = lose = 0
    for call in conn.execute("SELECT * FROM llm_call WHERE status='applied' AND output_json IS NOT NULL").fetchall():
        pack = conn.execute("SELECT * FROM veto_packet WHERE pack_id=?", (call["pack_id"],)).fetchone()
        if pack["market"] != market:
            continue
        content, resp = json.loads(pack["content_json"]), json.loads(call["output_json"])
        day = date.fromisoformat(pack["target_session"])
        base = {t["code"]: t for t in content["tickets"]}
        for a in resp.get("adjustments", []):
            t = base[a["code"]]
            removed = int(t["qty"]) - (a["qty"] if a["type"] == "reduce_buy_qty" else 0)
            vetoed += 1
            bars = md.hourly(a["code"], day)
            m = match_order(SimOrder("veto", a["code"], BUY, removed, dec(t["limit_price"])), bars, protocol, complete=md.session_complete(a["code"], day))
            if not m.fills:
                continue
            would_fill += 1
            filled = sum(f.qty for f in m.fills)
            px = m.fills[0].price
            fee = estimate(select_rule(fee_rules, market, BUY, day.isoformat()), [(Decimal(f.qty), f.price) for f in m.fills]).fee
            end = day
            for _ in range(horizon):
                try:
                    end = cal.next_session(market, end)
                except cal.CalendarError:
                    break
            close = md.close(a["code"], end)
            if close is None:
                would_fill -= 1                       # 无法评估：不计入分母，不当作盈亏
                continue
            if close * filled < px * filled + fee:
                lose += 1
    prec = Decimal(lose) / would_fill if would_fill >= MIN_SAMPLE else None
    return VetoPrecision(vetoed, would_fill, lose, prec, horizon, "样本不足" if prec is None else "事后模拟，不是承诺")
