"""视图之间共用的单元/行构造（持仓、交易、盈亏三类视图与「股票详情」共用同一份口径，不各写一套）。

- `cost_cells`：券商平均成本 / 摊薄成本 / 本地移动平均成本 / 浮动盈亏（沿用 holdings 的口径与文案）；
- `concentration` / `weight_cell`：占该币种持仓市值的比例（币种之间不相加）；
- `fill_row`：成交行（含费用归属，沿用 trades 的口径）；
- `sell_pnl_cell`：逐笔卖出的已实现盈亏单元（精确/估算/不可用，沿用 pnl 的口径）。
"""
from __future__ import annotations

from decimal import Decimal

from mystock2.instruments.code_map import currency_of
from mystock2.ledger.pnl import QUALITY_TEXT
from mystock2.web import common as C

ZERO = Decimal(0)
FEE_KIND_TEXT = {"commission": "佣金", "platform": "平台费", "tax": "税费", "stamp": "印花税", "settlement": "交收费"}


def cost_cells(code: str, qty: Decimal, px, sp, cp) -> dict:
    """三类成本并列、互不覆盖。px＝valuation.Price；sp＝最近快照的持仓行（可为 None）；cp＝ledger.pnl 的 CodePnl（可为 None）。"""
    ccy = currency_of(code)
    broker_cost = C.price_cell(C.dec_or_none(sp["average_cost"]), ccy, tag="券商平均成本") if sp is not None and C.dec_or_none(sp["average_cost"]) is not None else \
        C.na_cell("最近快照没有该标的的平均成本" if sp is not None else "没有快照")
    diluted = C.price_cell(C.dec_or_none(sp["diluted_cost"]), ccy, tag="券商摊薄成本", title="摊薄成本把已实现盈亏摊入持仓，可为负；它不是持仓的平均成本") \
        if sp is not None and C.dec_or_none(sp["diluted_cost"]) is not None else C.na_cell("最近快照没有摊薄成本")
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
    return {"broker_cost": broker_cost, "diluted_cost": diluted, "local_cost": local, "unrealized": unreal}


def concentration(positions: dict, prices: dict) -> tuple[dict[str, Decimal], dict[str, bool]]:
    """每个币种的已估值市值合计与「有缺行情」标记（集中度只在同币种内算）。"""
    mv_by_ccy: dict[str, Decimal] = {}
    missing: dict[str, bool] = {}
    for c, q in positions.items():
        ccy = currency_of(c)
        if prices[c].close is None:
            missing[ccy] = True
        else:
            mv_by_ccy[ccy] = mv_by_ccy.get(ccy, ZERO) + q * prices[c].close
    return mv_by_ccy, missing


def weight_cell(code: str, mv: Decimal | None, mv_by_ccy: dict, missing: dict) -> dict:
    ccy = currency_of(code)
    if mv is not None and not missing.get(ccy) and mv_by_ccy.get(ccy):
        return C.pct_cell(mv / mv_by_ccy[ccy])
    return C.na_cell("缺行情或无市值，无法算集中度")


def fill_row(f: dict) -> dict:
    """成交行：没有费用事件入账＝「费用未入账」，不记为 0；成交净现金流是现金流水，不是盈亏，不着色。"""
    ccy = f["currency"]
    if f["fees"]:
        fee_text = "；".join(f"{FEE_KIND_TEXT.get(i['kind'], i['kind'])} {C.fmt_money(i['amount'], i['currency'])}" for i in f["fees"])
        other = bool(f.get("fee_other_ccy"))
        fee = C.money_cell(f["fee_total"], ccy, title=fee_text + ("（另有非成交币种的费用，未并入合计）" if other else ""), tag="另有外币费用" if other else None)
        fee_detail = fee_text
        net = f["cash_delta"] - f["fee_total"]
    else:
        fee = C.na_cell("该成交没有费用事件入账（可能晚到），不记为 0")
        fee_detail = "未入账"
        net = None
    return {
        "event_at": f["event_at"], "code": f["code"], "side": C.text_cell("买入" if f["side"] == "BUY" else "卖出"),
        "qty": C.qty_cell(f["qty"]), "price": C.price_cell(f["price"], ccy),
        "notional": C.money_cell(abs(f["cash_delta"]), ccy),
        "fee": fee, "fee_detail": fee_detail,
        "net_cashflow": C.money_cell(net, ccy, sign=True, reason="费用未入账"),      # 现金流水：中性色，不着色
        "currency": ccy, "sources": f["sources"], "versions": f["versions"], "corrected": f["versions"] > 1,
        "pre_opening": f["pre_opening"], "deal_id": f["deal_id"] or "",
    }


def sell_pnl_cell(s) -> dict:
    """逐笔卖出的已实现盈亏（费用后）：精确不加标签，估算/部分/不可用带口径标签；没有成本证据＝不可用，不给数。"""
    if s.realized is None:
        return C.na_cell(s.note or "没有成本证据")
    return C.money_cell(s.realized, s.currency, colored=True, sign=True, tag=None if s.quality == "exact" else QUALITY_TEXT[s.quality], title=s.note or None)
