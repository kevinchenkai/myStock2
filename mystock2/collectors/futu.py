"""富途采集器（实施方案 WP2.3/2.7）。按官方文档与 V1 经验编写；2026-10-05 已在负责人授权下对真实 OpenD 首跑核对（见 docs/records/first-real-run）。

- 通过 `TradeApi` 协议注入：单测用假实现；真实实现 `FutuTradeApi` 惰性导入 `futu`，**只读查询**，**显式 acc_id 与 security_firm**（不硬编码），仅实盘。
- 历史成交：自选 80 天窗口分段（官方未规定窗口；这是工程选择）、限频间隔可配（与 V1 共享额度，须错峰）、单个市场/窗口失败只记 partial 与可重试范围。
- 成交 → FILL（规范键与 V1 导入相同，自然去重）；订单费用 → FEE（归属订单的**最后一笔成交**，聚合语义以档案为准）；
  资金流水：**必须由显式类型映射**决定入账方式，未知类型进待匹配队列，**不猜**；成交类/换汇类流水只用于对账，不入账（现金单一记账来源，T-20）。
- 本模块不下单、不解锁交易、不写 V1。
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol

from mystock2.collectors.v1_import import _local_to_utc
from mystock2.core.db import atomic
from mystock2.core.money import dec, to_db
from mystock2.core.timeutil import iso_utc, utc_now
from mystock2.instruments.code_map import CodeError, market_of
from mystock2.ledger.events import (
    EventDraft,
    IdentityInsufficient,
    LedgerConflict,
    LedgerError,
    SourceDraft,
    booked_keys_for_source,
    ensure_account,
    fill_key,
    flow_key,
    link_source,
    post_dividend,
    post_event,
    queue_pending,
)
from mystock2.ledger.opening import create_snapshot

WINDOW_DAYS = 80                    # 工程选择：官方默认窗口 90 天，留余量（V1 同）
FEE_BATCH = 400                     # order_fee_query 每次最多 400 个订单（官方文档，未实测）
PRICE_Q, QTY_Q = Decimal("0.0001"), Decimal("0.000001")

# 资金流水的入账方式（必须显式映射；值见 CASHFLOW_RULES：DEPOSIT / WITHDRAW / INTEREST / TAX / RECON_ONLY / DIVIDEND / DIVIDEND_WHT /
# ACCOUNT_FEE / EXTERNAL / EXTERNAL_PLAIN）。未映射的类型进待匹配队列。改映射后重放不会以新键再入账（已入账的流水号报冲突）。
# EXTERNAL_PLAIN＝只有「空备注＋整数金额」才按符号记外部存取，其余（补偿、奖励、IPO 退款等）进待匹配（审核 Q3，负责人 2026-10-06 同意）。
# DIVIDEND＝股息总额、DIVIDEND_WHT＝同日同标的预扣税（成对入账为 post_dividend 情形①；接口无除息日：应收与支付同日，是已声明的局限）。
_DIV_CODE = re.compile(r"\(([A-Z0-9.]+)\)\s*dividend", re.I)                       # 美股新格式：「… COM(MSFT) dividend, USD 0.91 per share」
_DIV_CODE_OLD = re.compile(r"^([A-Z][A-Z0-9.]*)\s+[\d.]+\s+SHARES\b")                  # 美股旧格式：「TSM 1.00000000 SHARES DIVIDENDS …」
_DIV_CODE_HK = re.compile(r"<SEHK\s+(\d+)\b")                                         # 港股：「… <SEHK 700 TENCENT> 11807 shares」


def dividend_code(remark: str, ccy: str) -> str | None:
    """从资金流水备注解析股息标的；认不出返回 None（进待匹配，不猜）。"""
    if ccy == "USD":
        for rx in (_DIV_CODE, _DIV_CODE_OLD):
            m = rx.search(remark)
            if m:
                return f"US.{m.group(1).upper()}"
        return None
    m = _DIV_CODE_HK.search(remark)
    return f"HK.{int(m.group(1)):05d}" if m else None
CashflowMap = dict[str, str]


class FutuApiError(RuntimeError):
    pass


class TradeApi(Protocol):
    def deals(self, acc_id: int, market: str, start: date, end: date) -> list[dict]: ...
    def orders(self, acc_id: int, market: str, start: date, end: date) -> list[dict]: ...
    def positions(self, acc_id: int, market: str) -> list[dict]: ...
    def funds(self, acc_id: int) -> dict[str, dict]: ...
    def order_fees(self, acc_id: int, order_ids: list[str]) -> dict[str, list[dict]]: ...
    def cash_flow(self, acc_id: int, clearing_date: date) -> list[dict]: ...


@dataclass
class CollectReport:
    kind: str
    ok: bool = True
    rows: int = 0
    inserted: int = 0
    duplicate: int = 0
    pending: int = 0
    conflicts: list[str] = field(default_factory=list)
    failed_scopes: list[str] = field(default_factory=list)       # 可重试范围
    recon_only: dict[str, Decimal] = field(default_factory=dict)  # 仅对账的流水汇总（币种:类型 → 金额）
    notes: list[str] = field(default_factory=list)


def _windows(start: date, end: date, days: int = WINDOW_DAYS):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _q(x, step: Decimal) -> Decimal:
    return Decimal(repr(float(x))).quantize(step, rounding=ROUND_HALF_EVEN)


def _missing(x) -> bool:
    """futu 的缺值是字符串 'N/A'（NoneDataValue），也可能是 NaN/None。"""
    return x is None or (isinstance(x, float) and x != x) or (isinstance(x, str) and x.strip().upper() in ("", "N/A", "NAN", "NONE"))


def _opt(x, step: Decimal, *, absolute: bool = False) -> str | None:
    """可缺失的数值字段：缺值→None；有值则量化（非有限数抛 ArithmeticError，由调用方按行处理）。"""
    if _missing(x):
        return None
    q = _q(abs(float(x)) if absolute else x, step)
    if not q.is_finite():
        raise ArithmeticError(f"非有限数值：{x!r}")
    return to_db(q)


def collect_deals(ledger, api: TradeApi, *, account_id: str, acc_id: int, markets: list[str], start: date, end: date, window_days: int = WINDOW_DAYS,
                  sleep: Callable[[float], None] = time.sleep, min_interval: float = 3.2) -> CollectReport:
    rep = CollectReport("deals")
    ensure_account(ledger, account_id, "futu", "REAL")
    for market in markets:
        for ws, we in _windows(start, end, window_days):
            sleep(min_interval)
            try:
                rows = api.deals(acc_id, market, ws, we)
            except Exception as exc:  # noqa: BLE001
                rep.ok = False
                rep.failed_scopes.append(f"{market} {ws}~{we}: {type(exc).__name__}: {exc}")
                continue
            for r in rows:
                rep.rows += 1
                src = SourceDraft("futu", str(r.get("deal_id") or ""),
                                  {k: str(v) for k, v in r.items() if k in ("deal_id", "order_id", "code", "side", "price", "qty", "create_time", "status")})
                try:
                    code = r["code"]
                    mk = market_of(code)
                    side = str(r["side"]).upper()
                    status = str(r.get("status") or "OK").upper()
                    if side not in ("BUY", "SELL"):                       # 卖空/买回（SELL_SHORT/BUY_BACK）等：不按买卖猜，进待匹配（审核 P1-4）
                        raise ValueError(f"成交方向 {side} 未支持")
                    if status not in ("OK", "NONE"):                     # 券商取消/改动的成交（CANCELLED/CHANGED）不能当成交入账
                        raise ValueError(f"成交状态 {status}（非 OK）")
                    price, qty = _q(r["price"], PRICE_Q), _q(abs(float(r["qty"])), QTY_Q)
                    at = _local_to_utc(mk, r["create_time"])
                    if qty == 0 or not price.is_finite():
                        raise ValueError(f"qty/price {qty}/{price}")
                except (KeyError, CodeError, ValueError, TypeError, ArithmeticError) as exc:
                    queue_pending(ledger, src, f"富途成交无法入账：{exc}")          # 不静默丢行：进待匹配，对账会列出
                    rep.pending += 1
                    rep.notes.append(f"skipped:{r.get('deal_id')}:{exc}")
                    continue
                signed = qty if side == "BUY" else -qty
                try:
                    res = post_event(ledger, EventDraft(
                        fill_key(account_id, str(r["deal_id"]) if r.get("deal_id") else None), account_id, "FILL", at, "HKD" if mk == "HK" else "USD",
                        code=code, price=str(price), qty_delta=str(signed), cash_delta=str(-(signed * price)), ref_deal_id=str(r["deal_id"]),
                        ref_order_id=str(r.get("order_id") or "") or None, note="futu"), source=src)
                    rep.inserted += res.status == "inserted"
                    rep.duplicate += res.status == "duplicate"
                except IdentityInsufficient:
                    queue_pending(ledger, src, "富途成交缺少 deal_id")
                    rep.pending += 1
                except LedgerConflict as exc:
                    rep.conflicts.append(f"{r.get('deal_id')}: {exc}")
                except LedgerError as exc:
                    rep.notes.append(f"invalid:{r.get('deal_id')}:{exc}")
    return rep


def collect_orders(ledger, api: TradeApi, *, account_id: str, acc_id: int, markets: list[str], start: date, end: date, window_days: int = WINDOW_DAYS,
                   sleep: Callable[[float], None] = time.sleep, min_interval: float = 3.2) -> CollectReport:
    """订单（意图，含已撤/失败）→ broker_order；只用于复盘，不进账本和式。同一订单再次到达则更新为较新的状态。名称顺带入 instrument_name（富途优先）。"""
    rep = CollectReport("orders")
    ensure_account(ledger, account_id, "futu", "REAL")
    now = iso_utc(utc_now())
    for market in markets:
        for ws, we in _windows(start, end, window_days):
            sleep(min_interval)
            try:
                rows = api.orders(acc_id, market, ws, we)
            except Exception as exc:  # noqa: BLE001
                rep.ok = False
                rep.failed_scopes.append(f"{market} {ws}~{we}: {type(exc).__name__}: {exc}")
                continue
            for r in rows:
                rep.rows += 1
                try:
                    code = r["code"]
                    mk = market_of(code)
                    side = str(r["side"]).upper()
                    if side not in ("BUY", "SELL") or not r.get("order_id"):
                        raise ValueError(f"side/order_id {side}/{r.get('order_id')}")
                    created = _local_to_utc(mk, r["create_time"])
                    updated = _local_to_utc(mk, r["updated_time"]) if r.get("updated_time") else None
                    oid = str(r["order_id"])
                    vals = dict(market=mk, code=code, side=side, order_type=str(r.get("order_type") or ""), status=str(r.get("status") or "UNKNOWN"),
                                price=_opt(r.get("price"), PRICE_Q), qty=_opt(r.get("qty"), QTY_Q, absolute=True), dealt_qty=_opt(r.get("dealt_qty"), QTY_Q, absolute=True),
                                dealt_avg_price=_opt(r.get("dealt_avg_price"), PRICE_Q) if r.get("dealt_avg_price") else None, created_at=created, updated_at=updated)
                except (KeyError, CodeError, ValueError, TypeError, ArithmeticError) as exc:     # 单行脏数据（如价格 NaN）只跳过该行（审核 C-04）
                    rep.notes.append(f"skipped:{r.get('order_id')}:{exc}")
                    continue
                with atomic(ledger):
                    cur = ledger.execute("SELECT status, updated_at, source FROM broker_order WHERE account_id=? AND order_id=?", (account_id, oid)).fetchone()
                    if cur is None:
                        ledger.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, dealt_qty, dealt_avg_price, created_at, "
                                       "updated_at, time_trust, source, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (account_id, oid, mk, code, side, vals["order_type"], vals["status"], vals["price"], vals["qty"], vals["dealt_qty"], vals["dealt_avg_price"],
                                        created, updated, "assumed_local_tz", "futu", now))
                        rep.inserted += 1
                    elif (updated or "") > (cur["updated_at"] or "") or (cur["status"] != vals["status"] and cur["source"] == "v1"):
                        ledger.execute("UPDATE broker_order SET status=?, dealt_qty=?, dealt_avg_price=?, updated_at=?, source='futu' WHERE account_id=? AND order_id=?",
                                       (vals["status"], vals["dealt_qty"], vals["dealt_avg_price"], updated, account_id, oid))
                        rep.inserted += 1
                    else:
                        rep.duplicate += 1
                if r.get("stock_name"):
                    upsert_names(ledger, {code: str(r["stock_name"])}, "futu")
    return rep


def upsert_names(ledger, names: dict[str, str], source: str) -> None:
    """标的名称（展示用）。富途直采优先于 V1；同来源取较新的。"""
    now = iso_utc(utc_now())
    with atomic(ledger):
        for code, name in names.items():
            if not name or name == "None":
                continue
            ledger.execute("INSERT INTO instrument_name(code, name, source, updated_at) VALUES (?,?,?,?) "
                           "ON CONFLICT(code) DO UPDATE SET name=excluded.name, source=excluded.source, updated_at=excluded.updated_at "
                           "WHERE instrument_name.source!='futu' OR excluded.source='futu'", (code, name, source, now))


def collect_snapshot(ledger, api: TradeApi, *, account_id: str, acc_id: int, markets: list[str], captured_at, sleep=time.sleep, min_interval: float = 3.2) -> CollectReport:
    """持仓（逐市场）＋逐币种现金 → account_snapshot（只追加、幂等）。**读取失败则不写快照**（缺失显式，不记零）。"""
    rep = CollectReport("snapshot")
    ensure_account(ledger, account_id, "futu", "REAL")
    positions: dict[str, dict] = {}
    try:
        names: dict[str, str] = {}
        for m in markets:
            sleep(min_interval)
            for p in api.positions(acc_id, m):
                try:
                    market_of(p["code"])
                except CodeError:                                # 期权等非股票代码：单列在回执里，不让整份快照失败（审核 C-06）
                    rep.notes.append(f"unsupported_position:{p.get('code')}")
                    continue
                if p.get("stock_name"):
                    names[p["code"]] = str(p["stock_name"])
                # 成本类字段缺值（futu 'N/A'）只置空该字段，不放弃整份快照（审核 C-03）；数量缺失则整份快照失败（不记零）
                positions[p["code"]] = {"qty": str(_q(p["qty"], QTY_Q)), "sellable_qty": _opt(p.get("can_sell_qty"), QTY_Q),
                                        "cost_basis": _opt(p.get("cost_price"), PRICE_Q) if p.get("cost_price") else None,     # 历史列：券商 cost_price＝摊薄成本（首跑核实，可为负）
                                        "average_cost": _opt(p.get("average_cost"), PRICE_Q) if p.get("average_cost") else None,
                                        "diluted_cost": _opt(p.get("diluted_cost"), PRICE_Q)}
        sleep(min_interval)
        funds = api.funds(acc_id)
        cash = {ccy.upper(): {k: _opt(v, Decimal("0.01")) for k, v in d.items()} for ccy, d in funds.items() if not _missing(d.get("cash"))}
    except Exception as exc:  # noqa: BLE001
        rep.ok = False
        rep.failed_scopes.append(f"snapshot: {type(exc).__name__}: {exc}")
        return rep
    upsert_names(ledger, names, "futu")
    create_snapshot(ledger, account_id, captured_at, "futu", {c: {k: v for k, v in p.items() if v is not None} for c, p in positions.items()},
                    {c: {k: v for k, v in d.items() if v is not None} for c, d in cash.items()})
    rep.rows = len(positions) + len(cash)
    rep.inserted = 1
    return rep


def collect_order_fees(ledger, api: TradeApi, *, account_id: str, acc_id: int, sleep=time.sleep, min_interval: float = 3.2,
                       assume_market_currency: bool = False) -> CollectReport:
    """为成交补订单费用。

    - **稳定身份**：费用的规范键是「订单＋费用项」（`fee:{acct}:order:{oid}:{item}`），不随成交到达顺序变化；
      元数据 `ref_deal_id` 记录采集时该订单的最后一笔成交（仅作归属说明，不参与冲突判断）。
      同一订单同一费用项重复采集：金额相同＝重复；金额变化（如又来了新成交使订单费变化）＝**冲突并报告**，不重复入账、不静默覆盖。
    - **币种不猜**：接口未给出费用币种时进入待匹配队列；仅在显式 `assume_market_currency=True`（首跑核实后）时按成交市场币种入账。
    """
    from mystock2.instruments.code_map import currency_of

    rep = CollectReport("order_fees")
    ensure_account(ledger, account_id, "futu", "REAL")
    rows = ledger.execute(
        "SELECT ref_order_id, ref_deal_id, code, event_at FROM ledger_event WHERE account_id=? AND event_type='FILL' AND ref_order_id IS NOT NULL ORDER BY event_at, ref_deal_id",
        (account_id,)).fetchall()
    last_deal: dict[str, sqlite3.Row] = {}
    for r in rows:
        last_deal[r["ref_order_id"]] = r
    have = {r["business_key"]: (r["currency"], dec(r["cash_delta"])) for r in ledger.execute(
        "SELECT business_key, currency, cash_delta FROM ledger_event WHERE account_id=? AND event_type='FEE'", (account_id,))}
    todo = list(last_deal)
    for i in range(0, len(todo), FEE_BATCH):
        chunk = todo[i:i + FEE_BATCH]
        sleep(min_interval)
        try:
            fees = api.order_fees(acc_id, chunk)
        except Exception as exc:  # noqa: BLE001
            rep.ok = False
            rep.failed_scopes.append(f"order_fees[{i}:{i + len(chunk)}]: {type(exc).__name__}: {exc}")
            continue
        for oid, items in fees.items():
            fill = last_deal.get(oid)
            if fill is None:
                continue
            prefix = f"fee:{account_id}:order:{oid}:"
            wanted: dict[str, tuple] = {}                         # 规范键 → (币种, 入账金额, 原始项, 来源)
            seen: dict[str, int] = {}
            for it in items:
                src = SourceDraft("futu", f"fee:{oid}:{it.get('item')}", {"order_id": oid, "item": str(it.get("item")), "amount": str(it.get("amount"))})
                try:
                    amount = dec(str(it["amount"]))
                except (KeyError, LedgerError, ArithmeticError, ValueError) as exc:
                    queue_pending(ledger, src, f"订单费用金额无法解析：{exc}")
                    rep.pending += 1
                    continue
                if amount == 0:
                    continue
                if amount < 0:                                    # 负费用项（返还/冲回）：方向不猜，进待匹配（审核 L-06）
                    queue_pending(ledger, src, f"订单费用为负（{amount}）：返还/冲回需人工核对，不按支出入账")
                    rep.pending += 1
                    continue
                item = str(it["item"]).lower().replace(" ", "_")
                seen[item] = seen.get(item, 0) + 1
                key = prefix + item + (f"#{seen[item]}" if seen[item] > 1 else "")     # 同一订单两条同名费用项：都保留，不当重复
                ccy = (it.get("currency") or "").upper() or (currency_of(fill["code"]) if assume_market_currency else "")
                if not ccy:
                    queue_pending(ledger, src, "订单费用缺少币种：未核实前不猜（首跑核对后可用 assume_market_currency）")
                    rep.pending += 1
                    continue
                wanted[key] = (ccy, -amount, it, src)
            booked = {k: v for k, v in have.items() if k.startswith(prefix)}
            if booked:
                same_amounts = sorted((c, a) for c, a in booked.values()) == sorted((w[0], w[1]) for w in wanted.values())
                if same_amounts:                                  # 金额构成与已入账完全一致：重复（费用项标题可能因语言等变化，审核 P1-5）
                    rep.duplicate += len(wanted)
                    if set(booked) != set(wanted):
                        rep.notes.append(f"fee_titles_changed:{oid}")
                    continue
                if not set(booked) <= set(wanted):                # 已入账的费用项在本次结果里找不到（标题变了且金额也变了）：不猜对应关系
                    rep.conflicts.append(f"{oid}: 费用项与已入账不一致（已入账 {sorted(booked)}，本次 {sorted(wanted)}）；请人工核对")
                    continue
            for key, (ccy, cash, it, src) in wanted.items():
                if key in booked:
                    if booked[key] != (ccy, cash):                # 同订单同费用项金额变了（如又有新成交）：冲突并报告，不覆盖不重复入账
                        rep.conflicts.append(f"{oid}/{it['item']}: 已入账 {booked[key][1]}，现为 {cash}；请人工更正")
                    else:
                        rep.duplicate += 1
                    continue
                try:
                    res = post_event(ledger, EventDraft(key, account_id, "FEE", _fill_time(ledger, account_id, fill["ref_deal_id"]), ccy, cash_delta=str(cash),
                                                        ref_deal_id=fill["ref_deal_id"], ref_order_id=oid, note="futu order_fee_query"), source=src)
                    rep.inserted += res.status == "inserted"
                    rep.duplicate += res.status == "duplicate"
                    have[key] = (ccy, cash)
                except LedgerConflict as exc:
                    rep.conflicts.append(f"{oid}/{it['item']}: {exc}")
                except LedgerError as exc:
                    rep.notes.append(f"invalid:{oid}/{it['item']}:{exc}")
    return rep


def _fill_time(ledger, account_id: str, deal_id: str) -> str:
    return ledger.execute("SELECT event_at FROM ledger_event WHERE account_id=? AND business_key=?", (account_id, fill_key(account_id, deal_id))).fetchone()["event_at"]


_MISSING_IDS = {"", "N/A", "NONE", "NAN", "NULL"}
CASHFLOW_RULES = ("DEPOSIT", "WITHDRAW", "INTEREST", "TAX", "RECON_ONLY", "DIVIDEND", "DIVIDEND_WHT", "ACCOUNT_FEE", "EXTERNAL", "EXTERNAL_PLAIN")


def _flow_id(f: dict) -> str | None:
    """流水号；futu 缺值给字符串 'N/A'（或 NaN）：一律视为缺号（进待匹配），不当成真号（审核 C-02）。"""
    v = f.get("cashflow_id")
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip()
    return None if s.upper() in _MISSING_IDS else s


def _flow_src(f: dict) -> SourceDraft:
    return SourceDraft("futu", f"cashflow:{_flow_id(f) or f.get('cashflow_id')}", {k: str(v) for k, v in f.items()})


def _rebooked(ledger, f: dict, key_ok: Callable[[str], bool]) -> str | None:
    """同一流水号已按别的业务键入账（映射改过后重放）：返回冲突说明；否则 None（审核 P1-6）。"""
    fid = _flow_id(f)
    if fid is None:
        return None
    other = sorted(k for k in booked_keys_for_source(ledger, "futu", f"cashflow:{fid}") if not key_ok(k))
    if other:
        return f"{fid}: 该流水已按 {other} 入账，现映射给出不同的入账方式；改映射后不能重放入账，请走更正流程"
    return None


def collect_cash_flows(ledger, api: TradeApi, *, account_id: str, acc_id: int, days: list[date], type_map: CashflowMap, sleep=time.sleep,
                       min_interval: float = 3.1) -> CollectReport:
    """逐日取资金流水（官方：证券账户逐 clearing_date 查询）。入账方式由显式 `type_map` 决定；未知类型进待匹配队列。

    - 单行解析失败（金额 'N/A'、备注解析出非法代码等）只让该行进待匹配，不中断其后的日期（审核 C-01）；
    - 同一流水号已按别的业务键入账（改映射后重放）→ 冲突，不再入账（审核 P1-6）；
    - 股息与预扣税：预扣税必须为负才配对（正数＝退税/冲回，不猜，进待匹配，审核 P1-3）；负数股息（冲回）进待匹配；
      成对入账后预扣税行的来源也挂到税事件上，它此前的待匹配项随之结清（审核 L-03）。
    """
    bad = sorted({r for r in type_map.values() if r not in CASHFLOW_RULES})
    if bad:
        raise ValueError(f"type_map 中的非法入账方式：{bad}")
    rep = CollectReport("cash_flows")
    ensure_account(ledger, account_id, "futu", "REAL")

    def pend(f: dict, reason: str) -> None:
        queue_pending(ledger, _flow_src(f), reason)
        rep.pending += 1

    for d in days:
        sleep(min_interval)
        try:
            flows = api.cash_flow(acc_id, d)
        except Exception as exc:  # noqa: BLE001
            rep.ok = False
            rep.failed_scopes.append(f"cash_flow {d}: {type(exc).__name__}: {exc}")
            continue
        divs: dict[tuple[str, str], dict] = {}                      # (标的, 币种) → {"gross": [f…], "wht": [f…]}
        for f in flows:
            rep.rows += 1
            try:
                _one_flow(ledger, f, d, account_id, type_map, rep, divs, pend)
            except (LedgerError, CodeError, ArithmeticError, KeyError, TypeError, ValueError) as exc:
                pend(f, f"资金流水无法解析：{type(exc).__name__}: {exc}")
        for (code, ccy), g in divs.items():
            _post_dividends(ledger, account_id, code, ccy, g["gross"], g["wht"], d, rep, pend)
    return rep


def _one_flow(ledger, f: dict, d: date, account_id: str, type_map: CashflowMap, rep: CollectReport, divs: dict, pend) -> None:
    ctype = str(f.get("cashflow_type"))
    rule = type_map.get(ctype)
    fid = _flow_id(f)
    if rule in ("DIVIDEND", "DIVIDEND_WHT"):
        ccy = str(f.get("currency", "")).upper()
        code = dividend_code(str(f.get("cashflow_remark", "")), ccy)
        if code is None:
            pend(f, f"{ctype}：无法从备注确定标的/币种（未核实格式，不猜）")
            return
        market_of(code)                                              # 解析出不合法的代码（如 ABC.WS）→ 该行进待匹配
        dec(str(f["cashflow_amount"]))
        divs.setdefault((code, ccy), {"gross": [], "wht": []})["gross" if rule == "DIVIDEND" else "wht"].append(f)
        return
    ccy = str(f.get("currency", "")).upper()
    if rule is None:
        pend(f, f"未映射的资金流水类型：{ctype}")
        return
    amount = dec(str(f["cashflow_amount"]))                          # 官方：正＝流入，负＝流出
    src = _flow_src(f)
    if rule == "ACCOUNT_FEE":                                       # ADR/公司行动/过户等账户级费用：无对应成交 → ADJUST(INVESTMENT)，计入业绩（不是外部流水）
        kind, etype, extra = "acctfee", "ADJUST", {"adjust_class": "INVESTMENT", "note": f"futu {ctype} {str(f.get('cashflow_remark', ''))[:80]}".strip()}
    else:
        if rule == "EXTERNAL_PLAIN":
            if str(f.get("cashflow_remark") or "").strip() or amount != amount.to_integral_value():
                pend(f, f"{ctype}：有备注或金额非整数，不按外部存取自动入账（EXTERNAL_PLAIN）")
                return
            rule = "EXTERNAL"
        if rule == "EXTERNAL":                                       # 方向由金额符号决定：正＝转入（DEPOSIT），负＝转出（WITHDRAW）；类型含义须已由负责人确认
            rule = "DEPOSIT" if amount > 0 else "WITHDRAW"
        if rule == "RECON_ONLY":                                     # 成交/换汇等已由其他来源记账：只用于对账
            key = f"{ccy}:{ctype}"
            rep.recon_only[key] = rep.recon_only.get(key, Decimal(0)) + amount
            return
        if (rule == "DEPOSIT" and amount <= 0) or (rule == "WITHDRAW" and amount >= 0):
            pend(f, f"{ctype} 的金额方向与映射 {rule} 不符")
            return
        kind, etype = {"DEPOSIT": "deposit", "WITHDRAW": "withdraw", "INTEREST": "interest", "TAX": "tax"}[rule], rule
        extra = {"ref_event_key": f"futu_cashflow:{fid}" if rule == "TAX" else None, "note": f"futu {ctype}"}
    try:
        key = flow_key(account_id, fid, kind)
    except IdentityInsufficient:
        pend(f, "资金流水缺少流水号")
        return
    clash = _rebooked(ledger, f, lambda k: k == key)
    if clash:
        rep.conflicts.append(clash)
        return
    try:
        res = post_event(ledger, EventDraft(key, account_id, etype, f"{f.get('clearing_date', d)}T00:00:00Z", ccy, cash_delta=str(amount), **extra), source=src)
        rep.inserted += res.status == "inserted"
        rep.duplicate += res.status == "duplicate"
    except LedgerConflict as exc:
        rep.conflicts.append(f"{fid}: {exc}")


def _post_dividends(ledger, account_id: str, code: str, ccy: str, grosses: list[dict], whts: list[dict], d: date, rep: CollectReport, pend) -> None:
    if not grosses:                                                  # 只有预扣税、没有股息：不入账，进待匹配
        for w_ in whts:
            pend(w_, "预扣税没有同日同标的股息总额")
        return
    if any(dec(str(g["cashflow_amount"])) <= 0 for g in grosses) or any(dec(str(w["cashflow_amount"])) >= 0 for w in whts):
        for f_ in grosses + whts:                                    # 股息冲回（负）或预扣税退回（正）：方向不猜，整组进待匹配
            pend(f_, "股息为负或预扣税为正（冲回/退税）：需人工核对，不按常规股息入账")
        return
    if len(whts) > 1 or (whts and len(grosses) > 1):                 # 无法唯一配对：不猜，全部进待匹配
        for f_ in grosses + whts:
            pend(f_, "同日同标的多笔股息/预扣税，无法唯一配对")
        return
    wht = whts[0] if whts else None
    for gross in grosses:                                            # 同日同标的两笔股息（如港股 F/D 与 S/D）各自成组
        at = f"{gross.get('clearing_date', d)}T00:00:00Z"
        group = f"{gross.get('clearing_date', d)}:{code}:{_flow_id(gross) or gross.get('cashflow_id')}"
        prefix = f"div:{account_id}:{group}:"
        clash = _rebooked(ledger, gross, lambda k, p=prefix: k.startswith(p)) or (_rebooked(ledger, wht, lambda k, p=prefix: k.startswith(p)) if wht else None)
        if clash:
            rep.conflicts.append(clash)
            continue
        try:
            res = post_dividend(ledger, account_id, group, code, ccy, accrual_at=at, gross=str(dec(str(gross["cashflow_amount"]))), payment_at=at,
                                withholding_tax=str(-dec(str(wht["cashflow_amount"]))) if wht else None, source=_flow_src(gross))
            rep.inserted += sum(r.status == "inserted" for r in res)
            rep.duplicate += sum(r.status == "duplicate" for r in res)
            if wht:
                tax = next(r for r in res if r.event_id.startswith(prefix + "tax#"))
                link_source(ledger, _flow_src(wht), tax.event_id)    # 预扣税行的证据挂到税事件；它先前的待匹配项随之结清
        except LedgerError as exc:
            rep.conflicts.append(f"dividend {code} {at}: {exc}")


class FileCashflowApi:
    """从 JSONL（每行一条原始资金流水，含 clearing_date）离线重放资金流水：重建数据库时不必重新逐日请求 OpenD（逐日 3 秒，约 35 分钟）。"""

    def __init__(self, path):
        import json
        from pathlib import Path
        self.by_day: dict[str, list[dict]] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                self.by_day.setdefault(str(r["clearing_date"]), []).append(r)

    def cash_flow(self, acc_id: int, clearing_date: date) -> list[dict]:
        return list(self.by_day.get(clearing_date.isoformat(), []))


# ---------------------------------------------------------------- 真实实现（已于 2026-10-05 首跑核实：见 docs/records/first-real-run）
class FutuTradeApi:
    """富途 OpenAPI 的只读封装。方法与字段名按官方文档/V1 经验编写，2026-10-05 首跑核对过；卖空/成交状态等字段未在真实数据中出现过。"""

    def __init__(self, host: str, port: int, security_firm: str):
        self.host, self.port, self.security_firm = host, port, security_firm

    def _ctx(self, market: str):
        import futu as ft
        mk = {"HK": ft.TrdMarket.HK, "US": ft.TrdMarket.US}[market]
        return ft.OpenSecTradeContext(filter_trdmarket=mk, host=self.host, port=self.port, security_firm=getattr(ft.SecurityFirm, self.security_firm))

    def deals(self, acc_id: int, market: str, start: date, end: date) -> list[dict]:
        import futu as ft
        ctx = self._ctx(market)
        try:
            ret, df = ctx.history_deal_list_query(code="", start=f"{start} 00:00:00", end=f"{end} 23:59:59", acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            return [{"deal_id": r.deal_id, "order_id": r.order_id, "code": r.code, "side": str(r.trd_side), "price": r.price, "qty": r.qty,
                     "create_time": r.create_time, "status": str(getattr(r, "status", "") or "") or None} for r in df.itertuples()] if df is not None and len(df) else []
        finally:
            ctx.close()

    def orders(self, acc_id: int, market: str, start: date, end: date) -> list[dict]:
        import futu as ft
        ctx = self._ctx(market)
        try:
            ret, df = ctx.history_order_list_query(status_filter_list=[], code="", start=f"{start} 00:00:00", end=f"{end} 23:59:59", acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            return [{"order_id": r.order_id, "code": r.code, "stock_name": getattr(r, "stock_name", None), "side": str(r.trd_side), "order_type": str(r.order_type),
                     "status": str(r.order_status), "price": r.price, "qty": r.qty, "dealt_qty": r.dealt_qty, "dealt_avg_price": r.dealt_avg_price,
                     "create_time": r.create_time, "updated_time": r.updated_time} for r in df.itertuples()] if df is not None and len(df) else []
        finally:
            ctx.close()

    def positions(self, acc_id: int, market: str) -> list[dict]:
        import futu as ft
        ctx = self._ctx(market)
        try:
            ret, df = ctx.position_list_query(acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            return [{"code": r.code, "stock_name": getattr(r, "stock_name", None), "qty": r.qty, "can_sell_qty": r.can_sell_qty, "cost_price": r.cost_price,
                     "average_cost": getattr(r, "average_cost", None), "diluted_cost": getattr(r, "diluted_cost", None)} for r in df.itertuples()] if df is not None and len(df) else []
        finally:
            ctx.close()

    def funds(self, acc_id: int) -> dict[str, dict]:
        import futu as ft
        ctx = self._ctx("HK")
        try:
            ret, df = ctx.accinfo_query(acc_id=acc_id, trd_env=ft.TrdEnv.REAL, refresh_cache=True)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            row = df.iloc[0]
            out = {}
            for ccy, col in (("HKD", "hk_cash"), ("USD", "us_cash"), ("CNH", "cn_cash")):
                if col in row and not _missing(row[col]):                  # 缺某币种时 futu 给 'N/A'（不是 NaN）
                    out[ccy] = {"cash": row[col]}
            return out
        finally:
            ctx.close()

    def order_fees(self, acc_id: int, order_ids: list[str]) -> dict[str, list[dict]]:
        import futu as ft
        ctx = self._ctx("HK")
        try:
            ret, df = ctx.order_fee_query(order_id_list=order_ids, acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            out: dict[str, list[dict]] = {}
            for r in df.itertuples():
                for item, amount in (r.fee_details or []):
                    out.setdefault(str(r.order_id), []).append({"item": item, "amount": amount, "currency": None})   # 币种字段未核实：缺则进待匹配，不猜
            return out
        finally:
            ctx.close()

    def cash_flow(self, acc_id: int, clearing_date: date) -> list[dict]:
        import futu as ft
        ctx = self._ctx("HK")
        try:
            ret, df = ctx.get_acc_cash_flow(clearing_date=str(clearing_date), acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            return df.to_dict("records") if df is not None and len(df) else []
        finally:
            ctx.close()
