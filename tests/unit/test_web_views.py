"""六个内置视图的业务口径（合成账本；LN-03…LN-06、T-01、T-07）。"""
from decimal import Decimal as D

import pytest
import yaml

from mystock2.core import db as dbmod
from mystock2.ledger.events import EventDraft, ensure_account, flow_key, post_event
from mystock2.ledger.opening import record_opening

from .test_web_fixtures import NOW, RECEIVED, build_demo_db, get_view, make_app, put_closes, put_fx

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture()
def client(tmp_path):
    return make_app(tmp_path).test_client()


def by(rows, key, value):
    return next(r for r in rows if r[key] == value)


# ------------------------------------------------------------------ 账户总览
def test_overview_is_per_currency_and_does_not_add_currencies_by_default(client):
    code, b = get_view(client, "account_overview")
    d = b["data"]
    assert d["base_ccy"] is None and d["total"] is None and d["rates"] == []
    usd, hkd = by(d["currencies"], "currency", "USD"), by(d["currencies"], "currency", "HKD")
    assert usd["cash"]["text"] == "60,197.50 USD" and usd["market_value"]["text"] == "9,540.00 USD"      # 90 × 106（未复权收盘）
    assert usd["equity"]["text"] == "69,737.50 USD" and hkd["equity"]["text"] == "121,795.00 HKD"
    assert all("converted" not in r for r in d["currencies"])
    assert all(c["ccy"] in ("USD", "HKD") for r in d["currencies"] for c in (r["cash"], r["market_value"], r["equity"]))
    assert usd["cash"].get("dir") is None                                                                    # 现金不着色


def test_overview_uses_unadjusted_close_not_adjusted(tmp_path):
    p = build_demo_db(tmp_path, quotes=False)
    from datetime import date

    from mystock2.market.bars import DailyBar, put_daily
    mk = dbmod.connect_writer(p, "market")
    put_daily(mk, [DailyBar("US.NVDA", date(2026, 3, 10), "106", "107", "105", "106", "50", "1")], source="s", received_at=RECEIVED)   # 复权价 50
    put_daily(mk, [DailyBar("HK.00700", date(2026, 3, 10), "316", "317", "315", "316", "300", "1")], source="s", received_at=RECEIVED)
    mk.close()
    d = get_view(make_app(tmp_path, p).test_client(), "account_overview")[1]["data"]
    assert by(d["currencies"], "currency", "USD")["market_value"]["text"] == "9,540.00 USD"


def test_overview_base_currency_totals_show_rate_source_and_time(client):
    d = get_view(client, "account_overview", base_ccy="HKD")[1]["data"]
    t = d["total"]
    # USD 权益 69,737.50 × 7.8 + HKD 权益 121,795 = 665,747.50
    assert t["equity"]["text"] == "665,747.50 HKD" and t["cash"]["text"] == "512,335.50 HKD" and t["unavailable"] == []
    usd = by(d["currencies"], "currency", "USD")
    assert usd["converted"]["equity"]["text"] == "543,952.50 HKD"
    r = by(d["rates"], "from", "USD")
    assert r["rate"]["text"] == "7.8" and "dir" not in r["rate"] and r["rate"]["fx"] is True        # 汇率中性色
    assert r["legs"][0]["source"] == "synthetic" and r["legs"][0]["rate_date"] == "2026-03-10" and r["legs"][0]["received_at"]


def test_overview_triangulates_through_usd_and_shows_path(client):
    d = get_view(client, "account_overview", base_ccy="CNY")[1]["data"]
    hk = by(d["rates"], "from", "HKD")
    assert hk["path"] == "HKD→USD→CNY" and len(hk["legs"]) == 2
    assert D(hk["rate"]["v"]) == D(1) / D("7.8") * D("7.2")
    assert not d["total"]["equity"].get("na")


def test_overview_missing_fx_makes_total_unavailable_not_partial(tmp_path):
    p = build_demo_db(tmp_path, fx=False)
    d = get_view(make_app(tmp_path, p).test_client(), "account_overview", base_ccy="HKD")[1]["data"]
    t = d["total"]
    assert t["equity"]["na"] is True and t["equity"]["text"] == "不可用" and t["cash"]["na"] is True
    assert [u["currency"] for u in t["unavailable"]] == ["USD"]
    assert by(d["rates"], "from", "USD")["rate"]["na"] is True
    assert by(d["currencies"], "currency", "USD")["converted"] is None
    assert by(d["currencies"], "currency", "USD")["equity"]["text"] == "69,737.50 USD"       # 原币种仍可查


def test_overview_stale_fx_beyond_limit_is_unavailable(tmp_path):
    p = build_demo_db(tmp_path, fx=False)
    mk = dbmod.connect_writer(p, "market")
    put_fx(mk, "USDHKD", "7.8", dates=["2026-03-03"])                                         # 8 天前
    mk.close()
    d = get_view(make_app(tmp_path, p).test_client(), "account_overview", base_ccy="HKD")[1]["data"]
    assert d["total"]["equity"]["na"] is True
    d2 = get_view(make_app(tmp_path, p).test_client(), "account_overview", base_ccy="HKD", fx_max_stale_days="10")[1]["data"]
    assert not d2["total"]["equity"].get("na") and by(d2["rates"], "from", "USD")["stale_days"] == 8


def test_overview_missing_quote_makes_market_value_and_equity_unavailable_not_zero(tmp_path):
    p = build_demo_db(tmp_path, quotes=False)
    mk = dbmod.connect_writer(p, "market")
    put_closes(mk, "HK.00700", {"2026-03-10": "316"})                                         # NVDA 没有任何行情
    mk.close()
    code, b = get_view(make_app(tmp_path, p).test_client(), "account_overview", base_ccy="HKD")
    d = b["data"]
    usd = by(d["currencies"], "currency", "USD")
    assert usd["market_value"]["na"] is True and usd["equity"]["na"] is True and usd["unvalued"] == ["US.NVDA"]
    assert usd["cash"]["text"] == "60,197.50 USD"                                             # 现金是账本事实，仍可见
    assert usd["market_value"]["title"].startswith("缺行情")
    assert by(d["currencies"], "currency", "HKD")["equity"]["text"] == "121,795.00 HKD"
    assert d["total"]["equity"]["na"] is True and not d["total"]["cash"].get("na")        # 合计权益不可用；合计现金可算
    assert by(d["positions"], "code", "US.NVDA")["market_value"]["na"] is True
    assert b["header"]["staleness"]["label"] == "未知"                                         # 行情时间未知 → 不显示新鲜


def test_overview_partial_and_stale_quotes_are_not_used_as_close(tmp_path):
    p = build_demo_db(tmp_path, quotes=False)
    mk = dbmod.connect_writer(p, "market")
    put_closes(mk, "US.NVDA", {"2026-03-09": "100"})
    put_closes(mk, "US.NVDA", {"2026-03-10": "999"}, quality="partial")                       # 当日 partial：不当收盘价
    put_closes(mk, "HK.00700", {"2026-03-10": "316"})
    mk.close()
    d = get_view(make_app(tmp_path, p).test_client(), "account_overview")[1]["data"]
    px = by(d["positions"], "code", "US.NVDA")["price"]
    assert px["v"] == "100" and px["tag"] == "陈旧 2026-03-09"                                  # 回退到上一个 ok 收盘并标陈旧


def test_overview_reconciliation_ok_and_mismatch_items(tmp_path):
    ok = get_view(make_app(tmp_path).test_client(), "account_overview")[1]["data"]["reconciliation"]
    assert ok["status"] == "ok" and ok["items"] == [] and ok["snapshot"]["source"] == "futu"
    (tmp_path / "mm").mkdir()
    p = build_demo_db(tmp_path / "mm", snapshot="mismatch")
    bad = get_view(make_app(tmp_path, p).test_client(), "account_overview")[1]["data"]["reconciliation"]
    assert bad["status"] == "mismatch"
    kinds = {i["kind"] for i in bad["items"]}
    assert kinds == {"持仓", "现金"} and any("US.NVDA" in i["text"] for i in bad["items"])


def test_overview_without_any_snapshot_is_unreconciled_not_ok(tmp_path):
    p = tmp_path / "ns.db"
    dbmod.migrate(p)
    led = dbmod.connect_writer(p, "ledger")
    ensure_account(led, "A1", "futu", "REAL", "USD")
    post_event(led, EventDraft(flow_key("A1", "d", "DEPOSIT"), "A1", "DEPOSIT", "2026-03-04T15:00:00Z", "USD", cash_delta="100"), received_at=RECEIVED)
    led.close()
    b = get_view(make_app(tmp_path, p).test_client(), "account_overview")[1]
    rc = b["data"]["reconciliation"]
    assert rc["status"] == "no_snapshot" and "未对账" in rc["label"] and rc["snapshot"] is None
    assert b["header"]["staleness"]["label"] == "未知"                      # 快照时间未知 → 不显示「新鲜」
    assert b["data"]["currencies"][0]["broker_cash"]["na"] is True


# ------------------------------------------------------------------ 持仓
def test_holdings_three_costs_side_by_side_never_overwritten(client):
    rows = get_view(client, "holdings")[1]["data"]["rows"]
    nv = by(rows, "code", "US.NVDA")
    assert nv["broker_cost"]["text"].startswith("85.00 USD") and nv["broker_cost"]["tag"] == "快照原值"
    assert nv["diluted_cost"]["na"] is True                                                     # 摊薄成本：没有就不可用
    assert nv["local_cost"]["v"] == "81.83181818181818181818181818181818181818" or nv["local_cost"]["text"].startswith("81.8318")
    assert nv["local_cost"]["tag"] == "估算"                                                     # 含开账快照成本 → 估算
    assert nv["broker_cost"]["v"] != nv["local_cost"]["v"]                                       # 互不覆盖
    tx = by(rows, "code", "HK.00700")
    assert tx["broker_cost"]["na"] is True and tx["local_cost"]["tag"] == "部分"                  # 开账持仓没有成本证据
    assert nv["order"]["text"] == "M6 提供" and tx["order"]["text"] == "M6 提供"


def test_holdings_quantity_vs_broker_and_concentration(client):
    rows = get_view(client, "holdings")[1]["data"]["rows"]
    nv = by(rows, "code", "US.NVDA")
    assert nv["qty"]["text"] == "90" and nv["broker_qty"]["text"] == "90" and nv["qty_match"] is True
    assert nv["market_value"]["text"] == "9,540.00 USD" and nv["weight"]["text"] == "100.00%"    # 占该币种持仓市值，币种之间不相加
    assert nv["unrealized"]["dir"] == "up" and nv["unrealized"]["tag"] == "估算"                  # (106−81.8318)×110? 见下
    # 浮动盈亏 = (106 − 平均成本 81.8318…) × 有成本证据的股数 90 = 2,175.14
    assert nv["unrealized"]["text"] == "+2,175.14 USD"


def test_holdings_roles_from_universe_and_missing_universe(tmp_path):
    u = tmp_path / "u.yaml"
    u.write_text(yaml.safe_dump({"instruments": [{"code": "US.NVDA", "tier": "trade", "max_weight": "0.3", "max_lots": 5},
                                                 {"code": "HK.00700", "tier": "core"}]}), encoding="utf-8")
    rows = get_view(make_app(tmp_path, universe_path=u).test_client(), "holdings")[1]["data"]["rows"]
    assert by(rows, "code", "US.NVDA")["role"]["text"] == "交易" and by(rows, "code", "HK.00700")["role"]["text"] == "核心"
    rows = get_view(make_app(tmp_path, universe_path=tmp_path / "missing.yaml").test_client(), "holdings")[1]
    assert rows["status"] == "ok" and all(r["role"]["text"] == "未配置" for r in rows["data"]["rows"])
    assert any("标的名单" in n for n in rows["header"]["notes"])


def test_holdings_mismatch_with_broker_snapshot_is_visible(tmp_path):
    p = build_demo_db(tmp_path, snapshot="mismatch")
    nv = by(get_view(make_app(tmp_path, p).test_client(), "holdings")[1]["data"]["rows"], "code", "US.NVDA")
    assert nv["qty_match"] is False and nv["broker_qty"]["text"] == "93"


# ------------------------------------------------------------------ 交易
def test_trades_fee_attribution_source_count_and_currency(client):
    d = get_view(client, "trades")[1]["data"]
    assert d["total"] == 5
    d1 = by(d["rows"], "deal_id", "d1")
    assert d1["price"]["text"] == "100.00 USD" and d1["notional"]["text"] == "1,000.00 USD" and d1["currency"] == "USD"
    assert d1["fee"]["text"] == "1.50 USD" and "佣金 1.00 USD" in d1["fee_detail"] and "平台费 0.50 USD" in d1["fee_detail"]
    assert d1["sources"] == 2                                                                  # 同一成交经两条通道到达，归并为一个事件、证据两条
    assert d1["net_cashflow"]["text"] == "-1,001.50 USD"
    hk = by(d["rows"], "deal_id", "d3")
    assert hk["currency"] == "HKD" and hk["fee"]["text"] == "5.00 HKD"


def test_trades_missing_fee_is_unavailable_and_net_cashflow_is_neutral(client):
    d = get_view(client, "trades")[1]["data"]
    d4 = by(d["rows"], "deal_id", "d4")
    assert d4["fee"]["na"] is True and d4["net_cashflow"]["na"] is True                        # 费用没入账 ≠ 0
    for r in d["rows"]:
        assert "dir" not in r["net_cashflow"]                                                  # 净现金流不着色（它不是盈亏）
    tot = {t["currency"]: t["amount"]["text"] for t in d["net_cashflow_totals"]}
    assert tot["USD"] == "+1,646.50 USD" and tot["HKD"] == "-31,005.00 HKD"                    # 不含无费用的 d4；币种分列


def test_trades_pre_opening_flag_and_filter_and_limit(client):
    d = get_view(client, "trades", code="US.NVDA")[1]["data"]
    assert {r["deal_id"] for r in d["rows"]} == {"d0", "d1", "d2"} and by(d["rows"], "deal_id", "d0")["pre_opening"] is True
    assert by(d["rows"], "deal_id", "d1")["pre_opening"] is False
    d = get_view(client, "trades", limit="2")[1]["data"]
    assert d["total"] == 5 and d["shown"] == 2 and d["rows"][0]["event_at"] >= d["rows"][1]["event_at"]


def test_trades_corrected_fill_shows_version(tmp_path):
    p = build_demo_db(tmp_path)
    led = dbmod.connect_writer(p, "ledger")
    from mystock2.ledger.events import correct_event
    correct_event(led, "fill:A1:d2", EventDraft("fill:A1:d2", "A1", "FILL", "2026-03-04T15:00:00Z", "USD", code="US.NVDA", price="111", qty_delta="-20",
                                                cash_delta="2220", ref_deal_id="d2"), "req-d2", received_at=RECEIVED)
    led.close()
    r = by(get_view(make_app(tmp_path, p).test_client(), "trades")[1]["data"]["rows"], "deal_id", "d2")
    assert r["corrected"] is True and r["versions"] == 3 and r["price"]["text"] == "111.00 USD"


# ------------------------------------------------------------------ 盈亏
def test_pnl_estimated_and_unavailable_flags(client):
    d = get_view(client, "pnl")[1]["data"]
    usd, hkd = by(d["summary"], "currency", "USD"), by(d["summary"], "currency", "HKD")
    # NVDA：开账 100 股成本 80（快照）＋买入 10@100（费用 1.5）；卖出 20@110（费用 1）：平均成本 (8000+1001.5)/110
    assert usd["realized_estimated"]["tag"] == "估算" and usd["realized_estimated"]["dir"] == "up"
    assert usd["realized_estimated"]["text"] == "+562.36 USD"
    assert usd["realized_exact"]["text"] == "0.00 USD"
    # 0700：开账 200 股没有成本证据 → 卖出的一部分不可用，不记零
    assert hkd["has_unavailable"] is True and hkd["unavailable_qty"]["text"] == "33.3333"
    s = by(d["sells"], "code", "HK.00700")
    assert s["quality"] == "partial" and s["quality_text"] == "部分不可用" and "无成本证据" in s["note"]


def test_pnl_pre_opening_sale_is_unavailable_T01(client):
    """T-01：开账日前的卖出不产生精确盈亏，显示「不可用」。"""
    d = get_view(client, "pnl")[1]["data"]
    assert [p["ref"] for p in d["pre_opening"]] == ["d0"]
    assert d["pre_opening"][0]["pnl"]["na"] is True
    assert all(s["ref"] != "d0" for s in d["sells"])                                           # 不在已实现盈亏里
    assert by(d["summary"], "currency", "USD")["pre_opening"] == 1


def test_pnl_without_any_cost_evidence_is_unavailable_not_zero(tmp_path):
    p = build_demo_db(tmp_path, opening_cost=False)
    d = get_view(make_app(tmp_path, p).test_client(), "pnl")[1]["data"]
    s = by(d["sells"], "code", "US.NVDA")
    assert s["quality"] == "partial" or s["quality"] == "unavailable"
    nv = by(d["by_code"], "code", "US.NVDA")
    # 开账持仓 100 股没有成本；之后买入 10 股有成本；卖出 20 按比例消耗，有证据的那部分算盈亏、其余不可用
    assert D(nv["unavailable_qty"]["v"]) > 0 and nv["unavailable_net_proceeds"]["tag"] == "净收入，非盈亏"


def test_pnl_net_cashflow_is_named_separately_and_not_called_profit(client):
    b = get_view(client, "pnl")[1]
    usd = by(b["data"]["summary"], "currency", "USD")
    flow = usd["trade_net_cashflow"]
    assert "dir" not in flow                                                                    # 中性色
    assert flow["text"] == "+1,646.50 USD"                                                      # −1001.5 + 2199 + 449（含开账前成交的现金流水事实）
    assert flow["text"] != usd["realized_estimated"]["text"]                                    # 与盈亏是两个不同的数
    assert not any("盈利" in n and "净现金流" in n for n in b["header"]["notes"] if "不是盈亏" not in n)
    assert any("不是盈亏" in n for n in b["header"]["notes"])


def test_pnl_code_filter(client):
    d = get_view(client, "pnl", code="HK.00700")[1]["data"]
    assert [c["code"] for c in d["by_code"]] == ["HK.00700"] and [s["currency"] for s in d["summary"]] == ["HKD"]


# ------------------------------------------------------------------ 资产趋势
def test_equity_trend_three_separately_named_curves(client):
    d = get_view(client, "equity_trend")[1]["data"]
    assert d["names"] == {"market_value": "持仓市值", "equity": "账户权益", "profit": "剔除外部资金流的收益"}
    usd = by(d["series"], "currency", "USD")
    assert len(set(d["names"].values())) == 3 and usd["names"] == d["names"]
    p = by(usd["points"], "date", "2026-03-04")
    assert p["market_value"] == "9900" and p["equity"] == "21097.5"                             # 90 × 110 ≠ 权益
    assert p["market_value"] != p["equity"] != p["profit"]


def test_equity_trend_deposit_day_not_shown_as_profit_in_view_T07(client):
    usd = by(get_view(client, "equity_trend")[1]["data"]["series"], "currency", "USD")
    pts = {p["date"]: p for p in usd["points"]}
    dep, prev = pts["2026-03-05"], pts["2026-03-04"]
    assert dep["flow"] == "50000" and usd["flows"][0]["date"] == "2026-03-05"
    assert D(dep["equity"]) - D(prev["equity"]) > 49000                                          # 权益被入金推高
    assert D(dep["profit"]) < D(prev["profit"])                                                  # 当天标的下跌（110→104）：收益是下降的
    assert D(dep["profit"]) - D(prev["profit"]) == D(-540)                                       # 90 股 × (104−110)


def test_equity_trend_marks_gap_instead_of_connecting(client):
    usd = by(get_view(client, "equity_trend")[1]["data"]["series"], "currency", "USD")
    gap = by(usd["points"], "date", "2026-03-09")
    assert gap["status"] == "gap" and gap["missing"] == ["US.NVDA"]
    assert gap["equity"] is None and gap["market_value"] is None and gap["profit"] is None
    assert usd["gaps"] == [{"from": "2026-03-09", "to": "2026-03-09", "missing": ["US.NVDA"], "days": 1}]
    hkd = by(get_view(client, "equity_trend")[1]["data"]["series"], "currency", "HKD")
    assert all(p["status"] == "ok" for p in hkd["points"])                                       # 港股行情完整，不受美股缺口影响


def test_equity_trend_currencies_are_separate_series(client):
    d = get_view(client, "equity_trend")[1]["data"]
    assert [s["currency"] for s in d["series"]] == ["HKD", "USD"]
    assert by(d["series"], "currency", "HKD")["latest"]["equity"]["ccy"] == "HKD"


def test_equity_trend_max_points_trims_display_but_keeps_baseline(client):
    usd = by(get_view(client, "equity_trend", max_points="5")[1]["data"]["series"], "currency", "USD")
    assert usd["shown_points"] == 5 and usd["total_points"] == 7 and usd["base_date"] == "2026-03-02"
    assert usd["points"][0]["date"] == "2026-03-04" and usd["points"][-1]["profit"] == "737.5"


def test_equity_trend_deposit_with_large_amount_and_falling_stock(tmp_path):
    """端到端 T-07：入金 100 万，同日标的下跌——曲线上入金不是收益。"""
    p = tmp_path / "t07.db"
    dbmod.migrate(p)
    led = dbmod.connect_writer(p, "ledger")
    ensure_account(led, "A1", "futu", "REAL", "USD")
    record_opening(led, "A1", "2026-03-02T00:00:00Z", {"US.NVDA": "100"}, {"USD": "1000"})
    post_event(led, EventDraft(flow_key("A1", "big", "DEPOSIT"), "A1", "DEPOSIT", "2026-03-04T15:00:00Z", "USD", cash_delta="1000000"), received_at=RECEIVED)
    led.close()
    mk = dbmod.connect_writer(p, "market")
    put_closes(mk, "US.NVDA", {"2026-03-02": "100", "2026-03-03": "100", "2026-03-04": "90", "2026-03-05": "90", "2026-03-06": "90", "2026-03-09": "90", "2026-03-10": "90"})
    mk.close()
    usd = by(get_view(make_app(tmp_path, p).test_client(), "equity_trend")[1]["data"]["series"], "currency", "USD")
    pts = {x["date"]: x for x in usd["points"]}
    assert D(pts["2026-03-04"]["equity"]) == 1_010_000 and D(pts["2026-03-04"]["profit"]) == -1000
    assert D(pts["2026-03-10"]["profit"]) == -1000
    assert pts["2026-03-04"]["flow"] == "1000000"


def test_equity_trend_uses_unadjusted_close(tmp_path):
    p = build_demo_db(tmp_path, quotes=False)
    from datetime import date

    from mystock2.market.bars import DailyBar, put_daily
    mk = dbmod.connect_writer(p, "market")
    for d, c in (("2026-03-02", "100"), ("2026-03-03", "100"), ("2026-03-04", "110"), ("2026-03-05", "104"), ("2026-03-06", "104"), ("2026-03-09", "104"), ("2026-03-10", "106")):
        put_daily(mk, [DailyBar("US.NVDA", date.fromisoformat(d), c, c, c, c, "1", "1")], source="s", received_at=RECEIVED)
    put_closes(mk, "HK.00700", {"2026-03-02": "300", "2026-03-03": "310", "2026-03-04": "312", "2026-03-05": "320", "2026-03-06": "318", "2026-03-09": "315", "2026-03-10": "316"})
    mk.close()
    usd = by(get_view(make_app(tmp_path, p).test_client(), "equity_trend")[1]["data"]["series"], "currency", "USD")
    assert by(usd["points"], "date", "2026-03-10")["market_value"] == "9540"                     # 90 × 106，不是 90 × 1


# ------------------------------------------------------------------ 外汇
def test_fx_paths_direct_inverse_and_via_usd(client):
    d = get_view(client, "fx")[1]["data"]
    p = {(r["from"], r["to"]): r for r in d["paths"]}
    assert len(p) == 6
    assert p[("USD", "HKD")]["path"] == "USD→HKD" and p[("USD", "HKD")]["direct"] is True and p[("USD", "HKD")]["rate"]["text"] == "7.8"
    assert p[("HKD", "USD")]["rate"]["text"] == "0.128205"                                       # 反向币对
    assert p[("HKD", "CNY")]["path"] == "HKD→USD→CNY" and p[("HKD", "CNY")]["direct"] is False
    assert all("dir" not in r["rate"] for r in d["paths"])
    assert p[("USD", "HKD")]["needed"] is True and p[("USD", "CNY")]["needed"] is False          # 账户只用到 USD/HKD


def test_fx_missing_rates_are_unavailable(tmp_path):
    p = build_demo_db(tmp_path, fx=False)
    mk = dbmod.connect_writer(p, "market")
    put_fx(mk, "USDHKD", "7.8")
    mk.close()
    d = get_view(make_app(tmp_path, p).test_client(), "fx")[1]["data"]
    paths = {(r["from"], r["to"]): r for r in d["paths"]}
    assert paths[("HKD", "CNY")]["status"] == "unavailable" and paths[("HKD", "CNY")]["rate"]["na"] is True
    assert paths[("USD", "HKD")]["status"] == "ok"


def test_fx_history_gaps_and_pair_param(client):
    d = get_view(client, "fx")[1]["data"]
    assert d["pair"] == "USDHKD" and [h["date"] for h in d["history"]][0] == "2026-03-02" and len(d["history"]) == 7
    assert d["gaps"] == []
    d2 = get_view(client, "fx", pair="USDCNY")[1]["data"]
    assert d2["history"][0]["rate"] == "7.2"
    assert get_view(client, "fx", pair="EURUSD")[0] == 400


def test_fx_header_unknown_when_no_rates(tmp_path):
    p = build_demo_db(tmp_path, fx=False)
    b = get_view(make_app(tmp_path, p).test_client(), "fx")[1]
    assert b["header"]["staleness"]["label"] == "未知" and b["data"]["history"] == []


# ------------------------------------------------------------------ 新鲜度
def test_every_builtin_view_has_freshness_header_and_known_when_data_known(client):
    for vid in ("account_overview", "holdings", "trades", "pnl", "equity_trend", "fx"):
        h = get_view(client, vid)[1]["header"]
        assert h["staleness"]["label"] == "新鲜" and h["event_at"] and h["collected_at"] and h["data_mode_label"], vid


def test_header_goes_unknown_when_ledger_has_no_events_time(tmp_path):
    p = tmp_path / "e.db"
    dbmod.migrate(p)
    led = dbmod.connect_writer(p, "ledger")
    ensure_account(led, "A1", "futu", "REAL", "USD")
    led.close()
    b = get_view(make_app(tmp_path, p).test_client(), "trades")[1]
    assert b["status"] == "ok" and b["data"]["rows"] == [] and b["header"]["staleness"]["label"] == "未知"
    assert "新鲜" not in b["header"]["staleness"]["text"]


def test_stale_ledger_is_marked_stale(tmp_path):
    from datetime import timedelta
    app = make_app(tmp_path, clock=lambda: NOW + timedelta(days=10))
    b = get_view(app.test_client(), "trades")[1]
    assert b["header"]["staleness"]["label"] == "陈旧"
