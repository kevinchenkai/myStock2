"""富途采集器（实施方案 WP2.3/2.7）。**按官方文档与 V1 经验编写，尚未对真实 OpenD/账户验证**（需负责人授权后首跑）。

- 通过 `TradeApi` 协议注入：单测用假实现；真实实现 `FutuTradeApi` 惰性导入 `futu`，**只读查询**，**显式 acc_id 与 security_firm**（不硬编码），仅实盘。
- 历史成交：自选 80 天窗口分段（官方未规定窗口；这是工程选择）、限频间隔可配（与 V1 共享额度，须错峰）、单个市场/窗口失败只记 partial 与可重试范围。
- 成交 → FILL（规范键与 V1 导入相同，自然去重）；订单费用 → FEE（归属订单的**最后一笔成交**，聚合语义以档案为准）；
  资金流水：**必须由显式类型映射**决定入账方式，未知类型进待匹配队列，**不猜**；成交类/换汇类流水只用于对账，不入账（现金单一记账来源，T-20）。
- 本模块不下单、不解锁交易、不写 V1。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol

from mystock2.collectors.v1_import import _local_to_utc
from mystock2.core.money import dec
from mystock2.instruments.code_map import CodeError, market_of
from mystock2.ledger.events import (
    EventDraft,
    IdentityInsufficient,
    LedgerConflict,
    LedgerError,
    SourceDraft,
    ensure_account,
    fee_key,
    fill_key,
    flow_key,
    post_event,
    queue_pending,
)
from mystock2.ledger.opening import create_snapshot

WINDOW_DAYS = 80                    # 工程选择：官方默认窗口 90 天，留余量（V1 同）
FEE_BATCH = 400                     # order_fee_query 每次最多 400 个订单（官方文档，未实测）
PRICE_Q, QTY_Q = Decimal("0.0001"), Decimal("0.000001")

# 资金流水的入账方式（必须显式映射；值：DEPOSIT / WITHDRAW / INTEREST / TAX / RECON_ONLY）。未映射的类型进待匹配队列。
CashflowMap = dict[str, str]


class FutuApiError(RuntimeError):
    pass


class TradeApi(Protocol):
    def deals(self, acc_id: int, market: str, start: date, end: date) -> list[dict]: ...
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
                try:
                    code = r["code"]
                    mk = market_of(code)
                    side = str(r["side"]).upper()
                    price, qty = _q(r["price"], PRICE_Q), _q(abs(float(r["qty"])), QTY_Q)
                    at = _local_to_utc(mk, r["create_time"])
                    if side not in ("BUY", "SELL") or qty == 0:
                        raise ValueError(f"side/qty {side}/{qty}")
                except (KeyError, CodeError, ValueError, TypeError) as exc:
                    rep.notes.append(f"skipped:{r.get('deal_id')}:{exc}")
                    continue
                signed = qty if side == "BUY" else -qty
                src = SourceDraft("futu", str(r.get("deal_id") or ""), {k: str(v) for k, v in r.items() if k in ("deal_id", "order_id", "code", "side", "price", "qty", "create_time")})
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


def collect_snapshot(ledger, api: TradeApi, *, account_id: str, acc_id: int, markets: list[str], captured_at, sleep=time.sleep, min_interval: float = 3.2) -> CollectReport:
    """持仓（逐市场）＋逐币种现金 → account_snapshot（只追加、幂等）。**读取失败则不写快照**（缺失显式，不记零）。"""
    rep = CollectReport("snapshot")
    ensure_account(ledger, account_id, "futu", "REAL")
    positions: dict[str, dict] = {}
    try:
        for m in markets:
            sleep(min_interval)
            for p in api.positions(acc_id, m):
                market_of(p["code"])
                positions[p["code"]] = {"qty": str(_q(p["qty"], QTY_Q)), "sellable_qty": str(_q(p["can_sell_qty"], QTY_Q)) if p.get("can_sell_qty") is not None else None,
                                        "cost_basis": str(_q(p["cost_price"], PRICE_Q)) if p.get("cost_price") else None}
        sleep(min_interval)
        funds = api.funds(acc_id)
    except Exception as exc:  # noqa: BLE001
        rep.ok = False
        rep.failed_scopes.append(f"snapshot: {type(exc).__name__}: {exc}")
        return rep
    cash = {ccy.upper(): {k: (str(_q(v, Decimal('0.01'))) if v is not None else None) for k, v in d.items()} for ccy, d in funds.items()}
    create_snapshot(ledger, account_id, captured_at, "futu", {c: {k: v for k, v in p.items() if v is not None} for c, p in positions.items()},
                    {c: {k: v for k, v in d.items() if v is not None} for c, d in cash.items()})
    rep.rows = len(positions) + len(cash)
    rep.inserted = 1
    return rep


def collect_order_fees(ledger, api: TradeApi, *, account_id: str, acc_id: int, sleep=time.sleep, min_interval: float = 3.2) -> CollectReport:
    """为尚无费用事件的成交补订单费用：每个订单的各项费用归属该订单的**最后一笔成交**（聚合语义以费用档案为准）。"""
    rep = CollectReport("order_fees")
    ensure_account(ledger, account_id, "futu", "REAL")
    rows = ledger.execute(
        "SELECT ref_order_id, ref_deal_id, event_at FROM ledger_event WHERE account_id=? AND event_type='FILL' AND ref_order_id IS NOT NULL ORDER BY event_at, ref_deal_id",
        (account_id,)).fetchall()
    last_deal: dict[str, str] = {}
    for r in rows:
        last_deal[r["ref_order_id"]] = r["ref_deal_id"]
    have = {r["ref_deal_id"] for r in ledger.execute("SELECT ref_deal_id FROM ledger_event WHERE account_id=? AND event_type='FEE'", (account_id,))}
    todo = [o for o, d in last_deal.items() if d not in have]
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
            deal = last_deal.get(oid)
            if not deal:
                continue
            for it in items:
                amount = dec(str(it["amount"]))
                if amount == 0:
                    continue
                try:
                    res = post_event(ledger, EventDraft(fee_key(account_id, deal, str(it["item"]).lower().replace(" ", "_")), account_id, "FEE",
                                                        _fill_time(ledger, account_id, deal), it.get("currency", "USD").upper(), cash_delta=str(-abs(amount)),
                                                        ref_deal_id=deal, ref_order_id=oid, note="futu order_fee_query"),
                                     source=SourceDraft("futu", f"fee:{oid}:{it['item']}", {"order_id": oid, "item": it["item"], "amount": str(it["amount"])}))
                    rep.inserted += res.status == "inserted"
                    rep.duplicate += res.status == "duplicate"
                except LedgerError as exc:
                    rep.conflicts.append(f"{oid}/{it['item']}: {exc}")
    return rep


def _fill_time(ledger, account_id: str, deal_id: str) -> str:
    return ledger.execute("SELECT event_at FROM ledger_event WHERE account_id=? AND business_key=?", (account_id, fill_key(account_id, deal_id))).fetchone()["event_at"]


def collect_cash_flows(ledger, api: TradeApi, *, account_id: str, acc_id: int, days: list[date], type_map: CashflowMap, sleep=time.sleep,
                       min_interval: float = 3.1) -> CollectReport:
    """逐日取资金流水（官方：证券账户逐 clearing_date 查询）。入账方式由显式 `type_map` 决定；未知类型进待匹配队列。"""
    rep = CollectReport("cash_flows")
    ensure_account(ledger, account_id, "futu", "REAL")
    for d in days:
        sleep(min_interval)
        try:
            flows = api.cash_flow(acc_id, d)
        except Exception as exc:  # noqa: BLE001
            rep.ok = False
            rep.failed_scopes.append(f"cash_flow {d}: {type(exc).__name__}: {exc}")
            continue
        for f in flows:
            rep.rows += 1
            ctype = str(f.get("cashflow_type"))
            rule = type_map.get(ctype)
            ccy = str(f.get("currency", "")).upper()
            amount = dec(str(f["cashflow_amount"]))          # 官方：正＝流入，负＝流出
            src = SourceDraft("futu", f"cashflow:{f.get('cashflow_id')}", {k: str(v) for k, v in f.items()})
            if rule is None:
                queue_pending(ledger, src, f"未映射的资金流水类型：{ctype}")
                rep.pending += 1
                continue
            if rule == "RECON_ONLY":                             # 成交/换汇等已由其他来源记账：只用于对账
                key = f"{ccy}:{ctype}"
                rep.recon_only[key] = rep.recon_only.get(key, Decimal(0)) + amount
                continue
            if rule not in ("DEPOSIT", "WITHDRAW", "INTEREST", "TAX"):
                raise ValueError(f"type_map 中的非法入账方式：{rule}")
            if (rule == "DEPOSIT" and amount <= 0) or (rule == "WITHDRAW" and amount >= 0):
                queue_pending(ledger, src, f"{ctype} 的金额方向与映射 {rule} 不符")
                rep.pending += 1
                continue
            try:
                kind = {"DEPOSIT": "deposit", "WITHDRAW": "withdraw", "INTEREST": "interest", "TAX": "tax"}[rule]
                res = post_event(ledger, EventDraft(flow_key(account_id, str(f.get("cashflow_id")) if f.get("cashflow_id") else None, kind), account_id, rule,
                                                    f"{f.get('clearing_date', d)}T00:00:00Z", ccy, cash_delta=str(amount),
                                                    ref_event_key=f"futu_cashflow:{f.get('cashflow_id')}" if rule == "TAX" else None, note=f"futu {ctype}"), source=src)
                rep.inserted += res.status == "inserted"
                rep.duplicate += res.status == "duplicate"
            except IdentityInsufficient:
                queue_pending(ledger, src, "资金流水缺少流水号")
                rep.pending += 1
            except LedgerError as exc:
                rep.conflicts.append(f"{f.get('cashflow_id')}: {exc}")
    return rep


# ---------------------------------------------------------------- 真实实现（未经验证）
class FutuTradeApi:
    """富途 OpenAPI 的只读封装。**未经真实验证**；方法与字段名按官方文档/V1 经验编写，首跑须在负责人授权下核对。"""

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
                     "create_time": r.create_time} for r in df.itertuples()] if df is not None and len(df) else []
        finally:
            ctx.close()

    def positions(self, acc_id: int, market: str) -> list[dict]:
        import futu as ft
        ctx = self._ctx(market)
        try:
            ret, df = ctx.position_list_query(acc_id=acc_id, trd_env=ft.TrdEnv.REAL)
            if ret != ft.RET_OK:
                raise FutuApiError(str(df))
            return [{"code": r.code, "qty": r.qty, "can_sell_qty": r.can_sell_qty, "cost_price": r.cost_price} for r in df.itertuples()] if df is not None and len(df) else []
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
                if col in row and row[col] == row[col]:
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
                    out.setdefault(str(r.order_id), []).append({"item": item, "amount": amount, "currency": "USD"})   # 币种字段未核实：须首跑核对
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
