"""资产趋势的纯计算（无 I/O；实施方案 LN-04、T-07、§6A.5）。

三条曲线分开命名，不得混用：
- **持仓市值**：Σ 持仓数量 × 未复权收盘价；
- **账户权益**：现金 + 持仓市值 + 应收（同币种；币种之间不相加）；
- **剔除外部资金流的收益**：自基准日起 (E_t − E_基准) − Δ累计外部资金流 − Δ累计换汇划转。
  入金当天权益上升、累计外部资金流同额上升，收益不变——入金不显示为收益（T-07）。
  换汇划转（FX 腿）是各币种桶之间的转移，不是外部资金流，但在**单币种**视图里同样不是该币种的收益，故一并剔除。

缺行情的日子**不插值、不连线**：该点 `status="gap"`，写明缺哪些标的。

`replay_states` 与 `mystock2.ledger.projection.project` 的语义一致（开账边界、拆股因子、冲销按被冲销类型归类、同刻先拆股），
但只扫一遍事件、在每个截止时点取状态，避免每个日期都重扫账本；一致性由测试对照 `project` 验证。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Iterable, Mapping, Sequence

from mystock2.core.money import dec
from mystock2.core.timeutil import ensure_utc
from mystock2.ledger.projection import EXTERNAL_TYPES, OPENING_TYPES


@dataclass
class LedgerState:
    positions: dict[str, Decimal] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)
    receivable: dict[str, Decimal] = field(default_factory=dict)
    external_flow: dict[str, Decimal] = field(default_factory=dict)     # 累计外部资金流（DEPOSIT/WITHDRAW/外部流 ADJUST）
    fx_net: dict[str, Decimal] = field(default_factory=dict)            # 累计换汇划转（FX 腿的现金）
    adjust_other: int = 0                                               # 非外部流的 ADJUST 笔数（性质不由本视图判断）


def _add(d: dict[str, Decimal], k: str, v: Decimal) -> None:
    d[k] = d.get(k, Decimal(0)) + v


def _effective_type(row) -> str:
    t = row["event_type"]
    if t == "REVERSAL":
        return (row["note"] or "").split()[1].split("#")[0]
    return t


def replay_states(rows: Sequence[Mapping], splits: Iterable[Mapping], opening_at: str | datetime | None,
                  cutoffs: Sequence[datetime]) -> list[LedgerState]:
    """在每个截止时点（升序）返回账本状态。rows 为 `projection.load_events` 的原始事件行（含冲销与各版本）。"""
    t0 = ensure_utc(opening_at) if opening_at is not None else None
    items: list[tuple] = []
    for i, r in enumerate(rows):
        items.append((ensure_utc(r["event_at"]), 1, i, "E", r))
    for i, s in enumerate(splits):
        items.append((ensure_utc(s["effective_at"]), 0, i, "S", s))
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    cuts = list(cutoffs)
    if any(a > b for a, b in zip(cuts, cuts[1:], strict=False)):
        raise ValueError("cutoffs 必须升序")

    pos: dict[str, Decimal] = {}
    st = LedgerState()
    out: list[LedgerState] = []
    i = 0
    with localcontext() as ctx:
        ctx.prec = 40
        for cut in cuts:
            cut = ensure_utc(cut)
            while i < len(items) and items[i][0] <= cut:
                dt, _, _, kind, obj = items[i]
                i += 1
                if kind == "S":
                    code = obj["code"]
                    if code in pos:
                        pos[code] = pos[code] * obj["ratio_num"] / obj["ratio_den"]
                    continue
                t = obj["event_type"]
                if t not in OPENING_TYPES and t_is_pre(t0, dt):
                    continue
                ccy, cash, recv, qty = obj["currency"], dec(obj["cash_delta"]), dec(obj["recv_delta"]), dec(obj["qty_delta"])
                et = _effective_type(obj)
                if cash:
                    _add(st.cash, ccy, cash)
                    if et in EXTERNAL_TYPES or (et == "ADJUST" and obj["adjust_class"] == "EXTERNAL_FLOW"):
                        _add(st.external_flow, ccy, cash)
                    elif et == "FX":
                        _add(st.fx_net, ccy, cash)
                if et == "ADJUST" and obj["adjust_class"] != "EXTERNAL_FLOW" and t == "ADJUST":
                    st.adjust_other += 1
                if recv:
                    _add(st.receivable, ccy, recv)
                if qty:
                    _add(pos, obj["code"], qty)
            out.append(LedgerState(
                positions={k: v for k, v in pos.items() if v != 0}, cash={k: v for k, v in st.cash.items() if v != 0},
                receivable={k: v for k, v in st.receivable.items() if v != 0}, external_flow=dict(st.external_flow),
                fx_net=dict(st.fx_net), adjust_other=st.adjust_other))
    return out


def t_is_pre(t0: datetime | None, dt: datetime) -> bool:
    return t0 is not None and dt <= t0


def build_equity_series(ccy: str, days: Sequence[date], states: Sequence[LedgerState], code_ccy: Mapping[str, str],
                        prices: Mapping[str, Mapping[str, Decimal]]) -> tuple[list[dict], str | None]:
    """单币种三条曲线，返回 (点列表, 基准日)。prices: {code: {session_date_iso: 未复权收盘价}}；days 与 states 一一对应。
    基准日＝第一个可估值的日子；没有任何可估值日子时为 None（收益全为 None）。"""
    pts: list[dict] = []
    for d, s in zip(days, states, strict=True):
        iso = d.isoformat()
        held = {c: q for c, q in s.positions.items() if code_ccy.get(c) == ccy}
        missing = sorted(c for c in held if iso not in prices.get(c, {}))
        cash, recv = s.cash.get(ccy, Decimal(0)), s.receivable.get(ccy, Decimal(0))
        ext, fx = s.external_flow.get(ccy, Decimal(0)), s.fx_net.get(ccy, Decimal(0))
        p: dict = {"date": iso, "status": "ok", "missing": [], "cash": cash, "receivable": recv, "external_cum": ext, "fx_cum": fx,
                   "market_value": None, "equity": None, "profit": None}
        if missing:
            p["status"] = "gap"
            p["missing"] = missing
        else:
            mv = sum((q * prices[c][iso] for c, q in held.items()), Decimal(0))
            p["market_value"], p["equity"] = mv, cash + mv + recv
        pts.append(p)
    base = next((p for p in pts if p["status"] == "ok"), None)
    prev_ext = Decimal(0)
    for p in pts:
        p["flow"] = p["external_cum"] - prev_ext            # 当日外部资金流（入金为正）
        prev_ext = p["external_cum"]
        if base is not None and p["status"] == "ok":
            p["profit"] = (p["equity"] - base["equity"]) - (p["external_cum"] - base["external_cum"]) - (p["fx_cum"] - base["fx_cum"])
    return pts, (base["date"] if base is not None else None)
