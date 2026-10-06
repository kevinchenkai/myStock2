"""单笔交易 AI 评价：发给模型的输入（脱敏摘要）与提示词。纯计算，只读库，不调用任何模型。

发出什么（白名单，见 `PAYLOAD_KEYS`）：这一笔成交的事实、成交前已知的行情背景、同一标的的前后成交、成交后的事后结果。
**不发**：账户号、成交/订单 id、现金与总权益、其他标的的持仓、AI 操作单的内容（只给「已有 N 张已冻结单」的计数）。
时间口径：`known_at_decision`（成交前已知）与 `same_day`（成交当日全天，下单时不可知）与 `hindsight`（事后）分开放，
提示词要求模型把「过程」和「结果」分开评价，避免用结果倒推「当时就该知道」。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from mystock2.core.money import dec
from mystock2.instruments.code_map import currency_of, market_of
from mystock2.market.bars import get_daily
from mystock2.replay.cards import ReviewCard, build_cards, fills_and_fees

PROMPT_VERSION = "trade-review-v1"
PAYLOAD_KEYS = ("instrument", "trade", "known_at_decision", "same_day", "hindsight", "gaps")
PRE_SESSIONS = 20          # 成交前给模型看的交易日数
PRIOR_TRADES = 12          # 同一标的更早的成交（最多）
LATER_TRADES = 5           # 同一标的之后的成交（最多，事后信息）


def _pct(x: Decimal | None, dp: int = 2) -> str | None:
    return None if x is None else f"{float(x) * 100:+.{dp}f}%"


def _num(x, dp: int = 4) -> str | None:
    if x is None:
        return None
    s = f"{float(dec(x)):.{dp}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")                    # 只去小数部分的尾 0（整数 1000 不能变成 1）
    return s or "0"


def _adj_factor(r) -> Decimal | None:
    try:
        c, a = dec(r["close"]), dec(r["adj_close"])
    except Exception:
        return None
    return a / c if c else None


def _pre_context(conn: sqlite3.Connection, code: str, d: date) -> dict:
    """成交日之前（不含当日）的行情背景；只用截至前一交易日收盘已知的数据。"""
    market = market_of(code)
    start = d - timedelta(days=420)
    rows = [r for r in get_daily(conn, code, start, d - timedelta(days=1)) if r["quality"] == "ok"]
    if not rows:
        return {"available": False, "note": "成交前没有可用的日线行情"}
    last = rows[-PRE_SESSIONS:]
    adj = [(r["session_date"], dec(r["adj_close"])) for r in rows if r["adj_close"] is not None]

    def ret(n: int) -> str | None:
        if len(adj) <= n:
            return None
        return _pct(adj[-1][1] / adj[-1 - n][1] - 1)

    vol = None
    if len(adj) > 21:
        rs = [math.log(float(adj[i][1] / adj[i - 1][1])) for i in range(len(adj) - 20, len(adj))]
        m = sum(rs) / len(rs)
        vol = _pct(Decimal(str(math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)))))
    win = [a for _, a in adj[-250:]]
    pos = None
    if len(win) >= 60 and max(win) > min(win):
        pos = _num((adj[-1][1] - min(win)) / (max(win) - min(win)), 2)
    out = {
        "available": True, "market": market, "last_session": rows[-1]["session_date"], "last_close": _num(rows[-1]["close"]),
        "return_5d": ret(5), "return_20d": ret(20), "return_60d": ret(60), "daily_volatility_20d": vol,
        "position_in_250d_range_0to1": pos, "range_sessions_used": len(win),
        "recent_sessions_format": "日期 开 高 低 收 成交量（未复权，最近一个在最后）",
        "recent_sessions": [f"{r['session_date']} {_num(r['open'])} {_num(r['high'])} {_num(r['low'])} {_num(r['close'])} {_num(r['volume'], 0)}" for r in last],
    }
    if len(rows) < 61:
        out["note"] = f"成交前只有 {len(rows)} 个交易日的行情，长周期指标可能缺失"
    return out


def _same_day(conn: sqlite3.Connection, c: ReviewCard) -> dict:
    r = get_daily(conn, c.code, c.local_date, c.local_date)
    if not r:
        return {"available": False}
    r = r[0]
    rp = (c.execution or {}).get("range_position")
    return {"available": True, "open": _num(r["open"]), "high": _num(r["high"]), "low": _num(r["low"]), "close": _num(r["close"]),
            "volume": _num(r["volume"], 0),
            "range_position_0to1": None if rp is None else _num(dec(rp), 2),
            "range_position_meaning": "买入：成交价越接近当日最低越小（越好）；卖出：越接近当日最高越小（越好）"}


def _trade_view(f: dict) -> dict:
    return {"date": f["date"].isoformat(), "side": "BUY" if f["qty"] > 0 else "SELL", "qty": _num(abs(f["qty"]), 0), "price": _num(f["price"])}


def build_payload(conn: sqlite3.Connection, account_id: str, deal_id: str, *, name: str | None = None) -> dict | None:
    """找不到这笔成交（或它是期初库存）返回 None。conn 只读即可。"""
    card = next((c for c in build_cards(conn, conn, account_id) if c.deal_id == deal_id), None)
    if card is None:
        return None
    fills, _ = fills_and_fees(conn, account_id)
    same = [f for f in fills if f["code"] == card.code and not f["opening"]]
    idx = next((i for i, f in enumerate(same) if f["deal_id"] == deal_id), None)
    prior = same[:idx][-PRIOR_TRADES:] if idx is not None else []
    later = same[idx + 1:][:LATER_TRADES] if idx is not None else []
    ccy = currency_of(card.code)
    notional = card.price * card.qty
    before, after = card.inventory_before, card.inventory_after
    if before == 0:
        change = "新建仓"
    elif after == 0:
        change = "清仓"
    else:
        change = _pct(after / before - 1, 1)
    # FIFO 先消耗最老的库存：迁移而来（无成本记录）的期初股份最老。卖出时若期初股份还有剩余，这笔卖出就用到了它们。
    done = same[:idx] if idx is not None else []
    opening_qty = before - sum((f["qty"] for f in done), Decimal(0))
    sold_before = sum((-f["qty"] for f in done if f["qty"] < 0), Decimal(0))
    opening_involved = card.side == "SELL" and max(Decimal(0), opening_qty - sold_before) > 0
    oc = card.outcome or {}
    hind: dict = {"note": "以下都是成交之后才知道的信息，只能用来评价结果，不能用来评价当时的决策过程"}
    if oc.get("status") == "ok":
        hind["price_change_after_trade_adjusted"] = {f"{n}_sessions": _pct(dec(v)) if v is not None else "未到期" for n, v in oc["horizons"].items()}
        if "max_adverse" in oc:
            hind["max_adverse_move_in_window"] = _pct(dec(oc["max_adverse"]))
            hind["max_favorable_move_in_window"] = _pct(dec(oc["max_favorable"]))
            hind["window_sessions"] = oc.get("window_days")
        hind["move_direction_note"] = "涨跌为成交后价格相对成交价的变化（复权）；对卖出而言，价格上涨意味着卖早了，不利/有利变动已按卖出方向取反"
    else:
        hind["available"] = False
    hind["later_trades_same_instrument"] = [_trade_view(f) for f in later]
    intents = [f"{i['action']}（记录于看过AI之{'后' if i['seen_ai'] else '前'}）" for i in card.evidence.get("intents", [])]
    tickets = card.evidence.get("tickets_existing", [])
    return {
        "instrument": {"code": card.code, "name": name, "market": market_of(card.code), "currency": ccy},
        "trade": {
            "date": card.local_date.isoformat(), "side": card.side, "qty": _num(card.qty, 0), "price": _num(card.price),
            "notional": _num(notional, 2), "fee": _num(card.fee, 2), "fee_pct_of_notional": None if card.fee is None or notional == 0 else _pct(card.fee / notional, 3),
            "inventory_before_shares": _num(before, 0), "inventory_after_shares": _num(after, 0), "position_change": change,
            "inventory_before_includes_migrated_opening_lots": opening_involved,
        },
        "known_at_decision": {
            "user_recorded_intents": intents or "没有记录事前意图（动机未记录）",
            "frozen_ai_tickets_before_trade": f"成交前已有 {len(tickets)} 张已冻结的 AI 单（内容不提供）" if tickets else "成交前没有已冻结的 AI 单",
            "prior_trades_same_instrument": [_trade_view(f) for f in prior],
            "market_context_before_trade": _pre_context(conn, card.code, card.local_date),
        },
        "same_day": _same_day(conn, card),
        "hindsight": hind,
        "gaps": list(card.gaps),
    }


def payload_for(conn: sqlite3.Connection, account_id: str, deal_id: str) -> dict | None:
    """带中文名的完整输入（Web 判断缓存是否过时、CLI 发请求都用它，保证两边哈希一致）。"""
    row = conn.execute("SELECT code FROM ledger_event WHERE account_id=? AND ref_deal_id=? AND event_type='FILL' LIMIT 1", (account_id, deal_id)).fetchone()
    name = None
    if row:
        nm = conn.execute("SELECT name FROM instrument_name WHERE code=?", (row["code"],)).fetchone()
        name = nm["name"] if nm else None
    return build_payload(conn, account_id, deal_id, name=name)


def input_hash(payload: dict) -> str:
    raw = json.dumps({"v": PROMPT_VERSION, "p": payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


RULES = """你是一位严谨、克制的个人交易复盘教练。下面是用户的**一笔真实成交**及其行情背景，请做事后评价。

# 规则（必须遵守）
1. 只使用「数据」里给出的信息。不要编造新闻、财报、公司事件、盘口、市场情绪，也不要写数据里没有的数字。需要引用一般常识时，写明「一般性背景」，且不据此下具体事实结论。不要联网。
2. 把「决策过程」和「事后结果」分开评价。过程只能依据 known_at_decision（含用户当时的成交史与仓位变化）；hindsight 只用来评价结果，以及过程是否被结果证实或证伪，不得用结果倒推「当时就该知道」。好结果不等于好决策，差结果也不等于差决策。
3. same_day 里的当日最高/最低/收盘，下单时不可知，只能用来评价执行位置（成交价在当日区间中的位置），不能当作决策依据。
4. 数据缺失（gaps、null、「未到期」、available=false）要直说「无法判断」，不要猜。只有这一笔样本，不要推断用户的整体水平或习惯。
5. 不预测未来走势，不给「现在该买/该卖」的建议。改进建议只写下次遇到类似情形可执行、可检验的**流程规则**，最多 3 条。
6. inventory_before_includes_migrated_opening_lots=true 表示这笔卖出消耗的库存里有迁移而来、没有成本记录的股份：不要评价盈亏或持仓成本。
7. 语气平实，不夸张，不使用感叹号；简体中文；全文不超过 600 字。

# 评分口径
- 过程评分 1–5：5＝顺应成交前可见的证据、仓位与价位合理；3＝中性或证据不足以判断；1＝与成交前可见证据明显相悖或仓位明显冲动。无法判断写「无法判断」。
- 结果评分 1–5：只看成交后的价格变化与不利/有利变动是否对这次操作有利；不足 1 个交易日写「未到期」。
- 置信度：高/中/低，取决于数据完整度。

# 输出格式（严格按此 Markdown 结构，不要加任何前后缀）
## 结论
一句话（不超过 40 字）。
过程评分：N/5 ｜ 结果评分：N/5 ｜ 置信度：高/中/低

## 决策过程（成交前已知）
- 2–4 条，每条引用数据里的具体字段或数值。

## 执行
- 成交价在当日区间的位置、仓位变化幅度、费用占比，各一句。

## 事后结果（以下是事后信息）
- 1–3 条。

## 做得好 / 可改进
- 做得好：1–2 条
- 改进：流程规则，最多 3 条

## 数据缺口与不确定性
- 列出缺失数据与它对结论的影响；没有就写「无」。
"""


def render_prompt(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    return f"{RULES}\n# 数据（JSON；金额币种见 instrument.currency；日期均为交易所本地交易日）\n{body}\n"
