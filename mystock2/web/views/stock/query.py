"""股票详情（只读）：单只标的的行情、持仓、走势、成交、股息、订单、资金流向、预测区间与档案。

- 参数 `code` 必填且须是完整的 US./HK. 代码；非法 → 400（`bad_param`）。
- **每个块独立**：某个来源缺失（没有行情、没有账户、没有档案、表不存在…）只让该块显示「不可用」，不让整个视图失败。
- 持仓/成本/成交/盈亏一律复用既有口径：`ledger.pnl`（移动平均成本）、`web/ledgerdata.py`、`web/valuation.py`、`web/rowcells.py`
  （与 holdings/trades/pnl 视图共用）；这里不另写一套。
- 订单（`broker_order`）是意图，不是成交：不进入持仓与盈亏；只用来看「下过什么单、是否撤单/失败」。
- 预测：**只读 `source_tag='rebuilt'`（事后重建，非前向证据）**；前向（forward）预测的数值根本不读取，只用「是否存在同一目标日的前向预测」
  这一个布尔信息做密封判断（该目标日尚无终值日线且无揭示记录 → 该行显示「已密封」，不含任何价位）。**不读取、不显示任何 AI 操作单（ticket）。**
- 52 周高低：近 250 个交易日日线（未复权，仅 quality='ok'）的最高/最低，并列档案里的来源值与 as_of；两者口径不同，不互相覆盖。
- 全程 Decimal，金额注明币种；缺失显示「不可用」，不记 0。时间只经 `common.now_of(params)`。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from mystock2.core import calendars as cal
from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import ensure_utc, to_market_time
from mystock2.instruments.code_map import CodeError, currency_of, market_of
from mystock2.ledger.pnl import QUALITY_TEXT, compute_realized_pnl
from mystock2.ledger.projection import effective_events, project
from mystock2.market.bars import get_daily
from mystock2.web import common as C
from mystock2.web import sealing
from mystock2.web.ledgerdata import load_trades
from mystock2.web.registry import ViewError
from mystock2.web.rowcells import concentration, cost_cells, fill_row, sell_pnl_cell, weight_cell
from mystock2.web.valuation import expected_session, latest_close

log = logging.getLogger("mystock2.web")
ZERO = Decimal(0)
WINDOW = 250                          # 52 周高低与走势图用的交易日数
TRADE_LIMIT = 30                      # 最近成交笔数
ORDER_LIMIT = 30
FLOW_DAYS = 20
DIV_LIMIT = 10
FAMILY_LABEL = {"baseline": "基线", "lgbm": "LightGBM+CQR"}
ORDER_STATUS = {
    "FILLED_ALL": ("全部成交", "ok"), "FILLED_PART": ("部分成交", "ok"), "CANCELLED_ALL": ("已撤单（未成交）", "cancel"),
    "CANCELLED_PART": ("部分成交后撤单", "cancel"), "FAILED": ("失败", "fail"), "DELETED": ("已删除", "cancel"), "DISABLED": ("已失效", "cancel"),
    "SUBMITTED": ("已提交", "open"), "SUBMITTING": ("提交中", "open"), "WAITING_SUBMIT": ("等待提交", "open"), "UNSUBMITTED": ("未提交", "open"),
    "TIMEOUT": ("超时", "fail"),
}
ORDER_TYPE = {"NORMAL": "限价单", "MARKET": "市价单", "ABSOLUTE_LIMIT": "绝对限价", "AUCTION": "竞价单", "AUCTION_LIMIT": "竞价限价单",
              "SPECIAL_LIMIT": "特别限价", "STOP": "止损单", "STOP_LIMIT": "止损限价单"}
PROFILE_ROWS = (
    ("market_cap_mm", "市值（百万）", "mm"), ("shares_mm", "总股本（百万股）", "plain"), ("trailing_pe", "市盈率（TTM）", "ratio"),
    ("forward_pe", "预期市盈率", "ratio"), ("price_to_book", "市净率", "ratio"), ("trailing_eps", "每股收益（TTM）", "eps"),
    ("dividend_yield", "股息率（来源原值）", "ratio"), ("beta", "Beta", "ratio"), ("lot_size", "每手股数", "plain"),
)


def _dec_or_none(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return dec(str(v))
    except Exception:                                    # noqa: BLE001 — 来源给了无法解析的文本：当作缺失，不猜
        return None


def _block(label: str, fn, *args) -> dict:
    """独立的块：来源缺失/表不存在/异常只让该块「不可用」。"""
    try:
        out = fn(*args)
    except C.ViewUnavailable as exc:
        return {"status": "unavailable", "reason": exc.message}
    except sqlite3.Error as exc:
        reason = "该来源的表不存在（库未迁移）" if "no such" in str(exc) else f"读取失败（{type(exc).__name__}）"
        return {"status": "unavailable", "reason": reason}
    except Exception as exc:                             # noqa: BLE001 — 一个块的计算错误不得拖垮整个弹窗
        log.exception("stock 块 %s 计算失败", label)
        return {"status": "unavailable", "reason": f"该块计算出错（{type(exc).__name__}），其他块不受影响"}
    out.setdefault("status", "ok")
    return out


class Ctx:
    """一次请求里被多个块共用的读取（惰性、只读）。"""

    def __init__(self, conn, params, code):
        self.conn, self.params, self.code = conn, params, code
        self.now = C.now_of(params)
        self.market = market_of(code)
        self.ccy = currency_of(code)
        self._cache: dict = {}

    def once(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ---- 行情
    @property
    def bars(self) -> list:
        """近 250 个有行情的交易日（quality='ok'，每日取最高 version）。"""
        def load():
            rows = get_daily(self.conn, self.code, self.now.date() - timedelta(days=400), self.now.date())
            return [r for r in rows if r["quality"] == "ok"][-WINDOW:]
        return self.once("bars", load)

    @property
    def px(self):
        return self.once("px", lambda: latest_close(self.conn, self.code, self.now))

    @property
    def expected(self):
        return self.once("expected", lambda: expected_session(self.market, self.now))

    # ---- 档案与名称
    @property
    def profile(self):
        return self.once("profile", lambda: self.conn.execute("SELECT * FROM instrument_profile WHERE code=?", (self.code,)).fetchone())

    # ---- 账本
    @property
    def account(self):
        return self.once("account", lambda: C.resolve_account(self.conn, self.params))

    @property
    def trades(self):
        return self.once("trades", lambda: load_trades(self.conn, self.account[0]["account_id"]))

    @property
    def pnl(self):
        def calc():
            t = self.trades
            return compute_realized_pnl([e for e in t.trade_events if e.code == self.code], t.opening_at)
        return self.once("pnl", calc)

    @property
    def fills(self) -> list[dict]:
        return self.once("fills", lambda: sorted((f for f in self.trades.fills if f["code"] == self.code),
                                                 key=lambda f: (f["event_at"], f["business_key"]), reverse=True))


# ------------------------------------------------------------------ ① 抬头
def _head(ctx: Ctx) -> dict:
    nm = ctx.conn.execute("SELECT name, source FROM instrument_name WHERE code=?", (ctx.code,)).fetchone()
    p = ctx.profile

    def txt(field):
        v = p[field] if p is not None else None
        return C.text_cell(str(v)) if v not in (None, "") else C.na_cell("档案没有该字段" if p is not None else "没有档案")
    return {
        "name": C.text_cell(nm["name"], title=f"来源 {nm['source']}") if nm is not None else C.na_cell("没有中文名记录"),
        "long_name": txt("long_name"), "sector": txt("sector"), "industry": txt("industry"), "exchange": txt("exchange"),
        "currency": C.text_cell(ctx.ccy, title="由代码推断（US→USD、HK→HKD）"),
        "profile_currency": C.text_cell(p["currency"]) if p is not None and p["currency"] else None,
        "market": ctx.market,
    }


# ------------------------------------------------------------------ ② 行情
def _prev_close_row(ctx: Ctx, last_row):
    bars = ctx.bars
    if len(bars) < 2 or bars[-1]["session_date"] != last_row["session_date"]:
        return None, "没有前一个有行情的交易日"
    prev = bars[-2]
    try:
        want = cal.prev_session(ctx.market, date.fromisoformat(last_row["session_date"])).isoformat()
    except (cal.CalendarError, ValueError):
        want = None
    if want is not None and prev["session_date"] != want:
        return None, f"前一交易日（{want}）缺行情"
    split = ctx.conn.execute("SELECT 1 FROM corporate_action WHERE code=? AND kind='SPLIT' AND substr(effective_at,1,10)>? AND substr(effective_at,1,10)<=?",
                             (ctx.code, prev["session_date"], last_row["session_date"])).fetchone()
    if split is not None:
        return None, "两个交易日之间有拆股，未复权价格的涨跌不可比"
    return prev, None


def _quote(ctx: Ctx) -> dict:
    px = ctx.px
    if px.close is None:
        raise C.ViewUnavailable("no_quote", px.reason or "没有行情")
    ccy = ctx.ccy
    last = next((r for r in reversed(ctx.bars) if r["session_date"] == px.session_date), None)
    prev, why = (None, "没有日线明细") if last is None else _prev_close_row(ctx, last)
    if prev is None:
        change, pct, prev_cell = C.na_cell(why), C.na_cell(why), C.na_cell(why)
    else:
        pc = dec(prev["close"])
        diff = px.close - pc
        prev_cell = C.price_cell(pc, ccy, title=f"前一交易日 {prev['session_date']} 收盘")
        change = C.money_cell(diff, ccy, colored=True, sign=True)
        pct = C.pct_cell(diff / pc, colored=True) if pc > 0 else C.na_cell("前收为 0")
    return {
        "close": C.price_cell(px.close, ccy, tag=f"陈旧 {px.session_date}" if px.stale else None,
                              title=f"收盘日 {px.session_date}（应有最近收盘日 {px.expected_session}）"),
        "session_date": px.session_date, "expected_session": px.expected_session, "stale": px.stale,
        "change": change, "change_pct": pct, "prev_close": prev_cell,
        "day_range": ({"low": C.price_cell(dec(last["low"]), ccy), "high": C.price_cell(dec(last["high"]), ccy)} if last is not None else None),
        "event_at": px.event_at, "received_at": px.received_at,
    }


def _range52(ctx: Ctx) -> dict:
    bars, ccy = ctx.bars, ctx.ccy
    p = ctx.profile
    ph = _dec_or_none(p["week52_high"]) if p is not None else None
    pl = _dec_or_none(p["week52_low"]) if p is not None else None
    prof = {
        "high": C.price_cell(ph, ccy) if ph is not None else C.na_cell("档案没有 52 周最高"),
        "low": C.price_cell(pl, ccy) if pl is not None else C.na_cell("档案没有 52 周最低"),
        "source": p["source"] if p is not None else None, "as_of": p["as_of"] if p is not None else None,
    }
    if not bars:
        return {"calc": None, "profile": prof, "reason": "没有日线行情，无法自算"}
    hi = max(bars, key=lambda r: dec(r["high"]))
    lo = min(bars, key=lambda r: dec(r["low"]))
    last_day = bars[-1]["session_date"]
    exp = ctx.expected
    stale = bool(exp and last_day < exp.isoformat())
    return {"calc": {"high": C.price_cell(dec(hi["high"]), ccy, title=f"出现在 {hi['session_date']}"), "high_date": hi["session_date"],
                     "low": C.price_cell(dec(lo["low"]), ccy, title=f"出现在 {lo['session_date']}"), "low_date": lo["session_date"],
                     "days": len(bars), "from": bars[0]["session_date"], "to": last_day, "partial": len(bars) < WINDOW,
                     "stale": stale},
            "profile": prof}


# ------------------------------------------------------------------ ③ 持仓
def _position(ctx: Ctx) -> dict:
    acct, accts = ctx.account
    aid = acct["account_id"]
    code, ccy = ctx.code, ctx.ccy
    proj = project(ctx.conn, aid, as_of=ctx.now)
    snap = C.latest_snapshot(ctx.conn, aid)
    sp = None
    if snap is not None:
        sp = ctx.conn.execute("SELECT * FROM snapshot_position WHERE snapshot_id=? AND code=?", (snap["snapshot_id"], code)).fetchone()
    cp = ctx.pnl.by_code.get(code)
    qty = proj.positions.get(code, ZERO)
    px = ctx.px
    held = qty != 0 or (sp is not None and dec(sp["qty"]) != 0)
    out = {"account_id": aid, "held": held, "snapshot": {"id": snap["snapshot_id"], "captured_at": snap["captured_at"]} if snap is not None else None,
           "accounts": [a["account_id"] for a in accts], "qty": C.qty_cell(qty)}
    if not held:
        out["message"] = "账本里当前没有该标的的持仓"
        return out
    mv = qty * px.close if px.close is not None else None
    prices = {c: (px if c == code else latest_close(ctx.conn, c, ctx.now)) for c in proj.positions}
    mv_by_ccy, missing = concentration(proj.positions, prices)
    sq = dec(sp["qty"]) if sp is not None else None
    out.update({
        "broker_qty": C.qty_cell(sq) if sq is not None else C.na_cell("没有快照"),
        "qty_match": None if sq is None else sq == qty,
        "market_value": C.money_cell(mv, ccy) if mv is not None else C.na_cell(px.reason or "缺行情"),
        "weight": weight_cell(code, mv, mv_by_ccy, missing) if qty != 0 else C.na_cell("账本数量为 0"),
        **cost_cells(code, qty, px, sp, cp),
    })
    return out


# ------------------------------------------------------------------ ④ 走势
def _agg_marks(ctx: Ctx, xs: set[str]) -> tuple[list[dict], int]:
    """账本成交按（市场本地日, 方向）合并成标记：数量合计、均价（Decimal）；日期不在图上的成交只计数。"""
    groups: dict[tuple[str, str], dict] = {}
    for f in ctx.fills:
        day = to_market_time(f["event_at"], ctx.market).date().isoformat()
        g = groups.setdefault((day, f["side"]), {"qty": ZERO, "notional": ZERO, "n": 0, "pre": True})
        g["qty"] += f["qty"]
        g["notional"] += f["qty"] * f["price"]
        g["n"] += 1
        g["pre"] = g["pre"] and bool(f["pre_opening"])
    marks, unplaced = [], 0
    for (day, side), g in sorted(groups.items()):
        if day not in xs:
            unplaced += g["n"]
            continue
        avg = g["notional"] / g["qty"]
        marks.append({"x": day, "side": side, "y": to_db(avg.quantize(Decimal("0.0001"))), "n": g["n"],
                      "label": f"{day} {'买入' if side == 'BUY' else '卖出'} {C.fmt_qty(g['qty'])} 股，均价 {C.fmt_price(avg)} {ctx.ccy}"
                               + (f"（{g['n']} 笔合并）" if g["n"] > 1 else "") + ("（开账前成交，只作描述）" if g["pre"] else "")})
    return marks, unplaced


def _chart(ctx: Ctx) -> dict:
    bars = ctx.bars
    if not bars:
        raise C.ViewUnavailable("no_bars", "没有日线行情，画不出走势")
    have = {r["session_date"]: r for r in bars}
    try:
        days = [d.isoformat() for d in cal.session_days(ctx.market, date.fromisoformat(bars[0]["session_date"]), date.fromisoformat(bars[-1]["session_date"]))]
    except (cal.CalendarError, ValueError):
        days = []
    xs = sorted(set(days) | set(have))
    gaps = {d: "该交易日缺行情（不插值、不记零）" for d in xs if d not in have}
    marks, unplaced, mark_note = [], 0, None
    try:
        marks, unplaced = _agg_marks(ctx, set(xs))
    except (C.ViewUnavailable, sqlite3.Error) as exc:
        mark_note = "买卖标记不可用：" + (exc.message if isinstance(exc, C.ViewUnavailable) else "账本读取失败")
    return {"dates": xs, "closes": [to_db(dec(have[d]["close"])) if d in have else None for d in xs], "gaps": gaps, "marks": marks,
            "unplaced_fills": unplaced, "mark_note": mark_note, "ccy": ctx.ccy, "days": len(bars), "from": xs[0], "to": xs[-1]}


# ------------------------------------------------------------------ ⑤ 成交 / 盈亏 / 股息
def _trades(ctx: Ctx) -> dict:
    fills = ctx.fills
    res = ctx.pnl
    sells = {s.ref: s for s in res.sells}
    rows = []
    for f in fills[:TRADE_LIMIT]:
        row = fill_row(f)
        if f["pre_opening"]:
            row["realized"] = C.na_cell("开账日及以前的成交没有成本证据，不产生盈亏")
            row["quality_text"] = "开账前"
        elif f["side"] == "SELL" and (f["deal_id"] or f["event_id"]) in sells:
            s = sells[f["deal_id"] or f["event_id"]]
            row["realized"] = sell_pnl_cell(s)
            row["quality_text"] = QUALITY_TEXT[s.quality]
        elif f["side"] == "SELL":
            row["realized"] = C.na_cell("没有对应的盈亏记录")
            row["quality_text"] = QUALITY_TEXT["unavailable"]
        else:
            row["realized"] = {"text": "—"}
            row["quality_text"] = "—"
        rows.append(row)
    cp = res.by_code.get(ctx.code)
    ccy = ctx.ccy
    summary = None
    if cp is not None:
        summary = {
            "realized_exact": C.money_cell(cp.realized_exact, ccy, colored=True, sign=True),
            "realized_estimated": C.money_cell(cp.realized_estimated, ccy, colored=True, sign=True, tag="估算") if cp.realized_estimated else C.text_cell("无"),
            "unavailable_qty": C.qty_cell(cp.unavailable_qty), "has_unavailable": cp.unavailable_qty > 0,
            "unavailable_net_proceeds": C.money_cell(cp.unavailable_net_proceeds, ccy, tag="净收入，非盈亏") if cp.unavailable_qty > 0 else None,
            "fees_total": C.money_cell(cp.fees_total, ccy), "buys": cp.buys, "sells": cp.sells,
        }
    return {"total": len(fills), "shown": len(rows), "rows": rows, "summary": summary, "pre_opening": len(res.pre_opening),
            "warnings": sorted(set(res.warnings) | set(ctx.trades.warnings))}


def _dividends(ctx: Ctx) -> dict:
    aid = ctx.account[0]["account_id"]
    t0 = ctx.trades.opening_at
    events = effective_events(ctx.conn, aid)

    def after_open(e):
        return t0 is None or e["event_at"] > t0
    pays = [e for e in events if e["event_type"] == "DIVIDEND_PAYMENT" and e["code"] == ctx.code]
    skipped = sum(1 for e in pays if not after_open(e))
    pays = [e for e in pays if after_open(e)]
    groups = {e["group_id"] for e in pays if e["group_id"]}
    taxes: dict[str, Decimal] = {}
    for e in events:
        if e["event_type"] == "TAX" and e["group_id"] in groups and after_open(e):
            taxes[e["group_id"]] = taxes.get(e["group_id"], ZERO) - dec(e["cash_delta"])
    short: dict[str, Decimal] = {}
    for e in events:
        if e["event_type"] == "DIVIDEND_SHORTFALL" and e["code"] == ctx.code and e["group_id"] in groups and after_open(e):
            short[e["group_id"]] = short.get(e["group_id"], ZERO) + dec(e["attrib_amount"] or "0")
    rows, tot_cash, tot_tax, tot_short = [], {}, {}, {}
    for e in sorted(pays, key=lambda e: (e["event_at"], e["event_id"]), reverse=True):
        ccy, g = e["currency"], e["group_id"]
        cash, tax, sf = dec(e["cash_delta"]), taxes.get(g, ZERO), short.get(g, ZERO)
        tot_cash[ccy] = tot_cash.get(ccy, ZERO) + cash
        tot_tax[ccy] = tot_tax.get(ccy, ZERO) + tax
        tot_short[ccy] = tot_short.get(ccy, ZERO) + sf
        rows.append({"event_at": e["event_at"], "cash": C.money_cell(cash, ccy), "tax": C.money_cell(tax, ccy) if tax else C.text_cell("无"),
                     "shortfall": C.money_cell(sf, ccy, title="只知净额：差额的性质（税/费/汇差）未知") if sf else C.text_cell("无"),
                     "net": C.money_cell(cash - tax, ccy)})
    totals = [{"currency": c, "cash": C.money_cell(v, c), "tax": C.money_cell(tot_tax[c], c), "shortfall": C.money_cell(tot_short[c], c),
               "net": C.money_cell(v - tot_tax[c], c)} for c, v in sorted(tot_cash.items())]
    return {"count": len(pays), "totals": totals, "rows": rows[:DIV_LIMIT], "skipped_pre_opening": skipped}


# ------------------------------------------------------------------ ⑥ 订单
def _dec_cell(v, ccy=None, kind="price", reason="来源没有这个字段"):
    d = _dec_or_none(v)
    if d is None:
        return C.na_cell(reason)
    if kind == "price":
        return C.price_cell(d, ccy) if d != 0 else C.na_cell("价格为 0（如市价单没有限价）")
    return C.qty_cell(d)


def _orders(ctx: Ctx) -> dict:
    conn, code = ctx.conn, ctx.code
    total = conn.execute("SELECT COUNT(*) FROM broker_order WHERE code=?", (code,)).fetchone()[0]
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM broker_order WHERE code=? GROUP BY status", (code,))}
    rows = []
    for r in conn.execute("SELECT * FROM broker_order WHERE code=? ORDER BY created_at DESC, order_id DESC LIMIT ?", (code, ORDER_LIMIT)):
        text, kind = ORDER_STATUS.get(r["status"], (r["status"], "other"))
        rows.append({
            "created_at": r["created_at"], "updated_at": r["updated_at"], "assumed_tz": r["time_trust"] != "exact",
            "side": C.text_cell("买入" if r["side"] == "BUY" else "卖出" if r["side"] == "SELL" else str(r["side"])),
            "order_type": ORDER_TYPE.get(r["order_type"] or "", r["order_type"] or C.UNAVAILABLE),
            "status": {"text": text, "kind": kind, "raw": r["status"]},
            "price": _dec_cell(r["price"], ctx.ccy), "qty": _dec_cell(r["qty"], kind="qty"), "dealt_qty": _dec_cell(r["dealt_qty"], kind="qty"),
            "dealt_avg_price": _dec_cell(r["dealt_avg_price"], ctx.ccy, reason="没有成交均价"), "source": r["source"], "account_id": r["account_id"],
        })
    return {"total": total, "shown": len(rows), "rows": rows,
            "by_status": [{"status": ORDER_STATUS.get(s, (s, ""))[0], "raw": s, "count": n} for s, n in sorted(counts.items())]}


# ------------------------------------------------------------------ ⑦ 资金流向
FLOW_FIELDS = (("in_flow", "净流入合计"), ("main_in_flow", "主力净流入"), ("super_in_flow", "超大单"), ("big_in_flow", "大单"),
               ("mid_in_flow", "中单"), ("sml_in_flow", "小单"))


def _flows(ctx: Ctx) -> dict:
    conn, ccy = ctx.conn, ctx.ccy
    dates = [r[0] for r in conn.execute("SELECT DISTINCT session_date FROM capital_flow_daily WHERE code=? ORDER BY session_date DESC LIMIT ?", (ctx.code, FLOW_DAYS))]
    if not dates:
        raise C.ViewUnavailable("no_flow", "没有该标的的资金流向记录")
    marks = ",".join("?" * len(dates))
    raw = conn.execute(f"SELECT * FROM capital_flow_daily WHERE code=? AND session_date IN ({marks}) ORDER BY session_date DESC, source", [ctx.code, *dates]).fetchall()
    rows, sums = [], {}
    for r in raw:
        row = {"date": r["session_date"], "source": r["source"], "received_at": r["received_at"]}
        for key, _ in FLOW_FIELDS:
            d = _dec_or_none(r[key])
            row[key] = C.money_cell(d, ccy, colored=True, sign=True, reason="来源没有这个字段") if d is not None else C.na_cell("来源没有这个字段")
        rows.append(row)
        agg = sums.setdefault(r["source"], {"days": 0, "main": ZERO, "main_n": 0, "all": ZERO, "all_n": 0})
        agg["days"] += 1
        for k, f in (("main", "main_in_flow"), ("all", "in_flow")):
            d = _dec_or_none(r[f])
            if d is not None:
                agg[k] += d
                agg[k + "_n"] += 1
    totals = [{"source": s, "days": a["days"],
               "main_in_flow": C.money_cell(a["main"], ccy, colored=True, sign=True) if a["main_n"] else C.na_cell("没有主力净流入数据"), "main_days": a["main_n"],
               "in_flow": C.money_cell(a["all"], ccy, colored=True, sign=True) if a["all_n"] else C.na_cell("没有净流入合计数据"), "in_days": a["all_n"]}
              for s, a in sorted(sums.items())]
    exp = ctx.expected
    latest = dates[0]
    return {"rows": rows, "totals": totals, "days": len(dates), "latest": latest, "stale": bool(exp and latest < exp.isoformat()), "ccy": ccy,
            "expected_session": exp.isoformat() if exp else None}


# ------------------------------------------------------------------ ⑧ 预测（只读事后重建）
def _family(model_version: str) -> str | None:
    mv = (model_version or "").lower().replace("_", "-")
    return "baseline" if mv.startswith("naive") else "lgbm" if mv.startswith("lgbm") else None


def _target_settled(ctx: Ctx, target: str) -> dict | None:
    r = ctx.conn.execute("SELECT low, high, received_at FROM quote_daily WHERE code=? AND session_date=? AND quality='ok' ORDER BY version DESC LIMIT 1",
                         (ctx.code, target)).fetchone()
    return r if r is not None and ensure_utc(r["received_at"]) <= ensure_utc(ctx.now) else None


def _target_revealed(ctx: Ctx, target: str) -> bool:
    batches = [r["batch_id"] for r in ctx.conn.execute("SELECT DISTINCT batch_id FROM intent_exposure WHERE market=? AND target_session=?", (ctx.market, target))]
    return any(sealing.reveal_info(ctx.conn, b, ctx.market, target, ctx.now) is not None for b in batches)


def _forecast(ctx: Ctx) -> dict:
    conn, ccy = ctx.conn, ctx.ccy
    rows = conn.execute("SELECT as_of_session, target_session, model_version, y_low, y_high, low_price, high_price, generated_at, input_cutoff_at "
                        "FROM prediction_version WHERE code=? AND source_tag='rebuilt' ORDER BY as_of_session, generated_at, rowid", (ctx.code,)).fetchall()
    if not rows:
        raise C.ViewUnavailable("no_prediction", "没有该标的的事后重建预测")
    fwd_targets = {r[0] for r in conn.execute("SELECT DISTINCT target_session FROM prediction_version WHERE code=? AND source_tag='forward'", (ctx.code,))}
    best: dict[str, sqlite3.Row] = {}
    for r in rows:                                                  # 已按 as_of、generated_at 升序：后者覆盖前者＝每个模型取最新一条
        best[_family(r["model_version"]) or r["model_version"]] = r
    out = []
    for fam in sorted(best, key=lambda f: (f not in FAMILY_LABEL, f)):
        r = best[fam]
        item = {"model": FAMILY_LABEL.get(fam, fam), "model_version": r["model_version"], "as_of": r["as_of_session"], "target": r["target_session"],
                "generated_at": r["generated_at"], "tag": "事后重建、非前向证据"}
        settled = _target_settled(ctx, r["target_session"])
        if r["target_session"] in fwd_targets and settled is None and not _target_revealed(ctx, r["target_session"]):
            item.update({"sealed": True, "note": f"目标日 {r['target_session']} 尚无终值日线、没有揭示记录，且存在前向预测：按 §6A.2 只显示「已密封」，不含价位。"})
        else:
            item.update({"sealed": False,
                         "low": dict(C.price_cell(dec(r["low_price"]), ccy), text=f"{C.fmt_decimal(dec(r['low_price']), 2)} {ccy}"),
                         "high": dict(C.price_cell(dec(r["high_price"]), ccy), text=f"{C.fmt_decimal(dec(r['high_price']), 2)} {ccy}"),
                         "rel_low": C.pct_cell(dec(r["y_low"]), colored=True), "rel_high": C.pct_cell(dec(r["y_high"]), colored=True),
                         "actual": ({"low": C.price_cell(dec(settled["low"]), ccy), "high": C.price_cell(dec(settled["high"]), ccy)} if settled is not None else None)})
        out.append(item)
    return {"rows": out, "total_rebuilt": len(rows)}


# ------------------------------------------------------------------ ⑨ 档案
def _fmt_profile(kind: str, d: Decimal, ccy: str) -> str:
    if kind == "mm":
        return f"{C.fmt_decimal(d, 2)} 百万 {ccy}"
    if kind == "eps":
        return f"{C.fmt_decimal(d, 2)} {ccy}"
    if kind == "plain":
        return C.fmt_decimal(d, 2, trim=True)
    return C.fmt_decimal(d, 4, trim=True)


def _profile(ctx: Ctx) -> dict:
    p = ctx.profile
    if p is None:
        raise C.ViewUnavailable("no_profile", "没有该标的的档案")
    ccy = (p["currency"] or ctx.ccy).upper()
    items = []
    for key, label, kind in PROFILE_ROWS:
        d = _dec_or_none(p[key])
        if d is None:
            cell = C.na_cell("来源没有这个字段" if p[key] in (None, "") else "来源给出的值无法解析")
        else:
            cell = {"text": _fmt_profile(kind, d, ccy), "v": to_db(d)}
        items.append({"key": key, "label": label, "cell": cell})
    web = p["website"]
    return {"items": items, "website": web if web else None, "source": p["source"], "as_of": p["as_of"], "updated_at": p["updated_at"],
            "ccy": ccy, "ccy_inferred": not p["currency"]}


# ------------------------------------------------------------------ 入口
def run(conn, params):
    code = (params.get("code") or "").strip()
    if not code:
        raise ViewError("缺少必填参数 code（完整代码，如 US.NVDA、HK.00700）")
    try:
        market_of(code)
    except CodeError:
        raise ViewError(f"参数 code 不是合法的完整代码：{code!r}（应如 US.NVDA、HK.00700）") from None
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"account", "ledger_event", "quote_daily"} <= have:       # 整库没迁移：整体不可用（各块的缺表只影响该块）
        raise C.ViewUnavailable("schema_missing", "库结构不完整，请先 db migrate")
    ctx = Ctx(conn, params, code)
    blocks = {
        "head": _block("head", _head, ctx), "quote": _block("quote", _quote, ctx), "range52": _block("range52", _range52, ctx),
        "position": _block("position", _position, ctx), "chart": _block("chart", _chart, ctx),
        "trades": _block("trades", _trades, ctx), "dividends": _block("dividends", _dividends, ctx),
        "orders": _block("orders", _orders, ctx), "flows": _block("flows", _flows, ctx),
        "forecast": _block("forecast", _forecast, ctx), "profile": _block("profile", _profile, ctx),
    }
    srcs = []
    if blocks["quote"]["status"] == "ok":
        srcs.append(C.source("日线行情（未复权收盘）", blocks["quote"]["event_at"], blocks["quote"]["received_at"]))
    aid = None
    if blocks["position"]["status"] == "ok" or blocks["trades"]["status"] == "ok":
        aid = ctx.account[0]["account_id"]
        srcs.append(C.ledger_source(conn, aid))
    notes = ["各块相互独立：来源缺失只让该块显示「不可用」", "金额均注明币种；订单是意图、不是成交；预测只显示事后重建（非前向证据）"]
    out = {"code": code, "currency": ctx.ccy, "market": ctx.market, "account_id": aid, **blocks, "_freshness": C.freshness(srcs, notes)}
    if aid is not None:
        out["accounts"] = [a["account_id"] for a in ctx.account[1]]
    return out
