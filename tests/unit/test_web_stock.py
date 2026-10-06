"""M3d：标的中文名（code_name）与股票详情视图（stock）的测试。全部合成数据（合成测试值；不含任何真实信息）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from mystock2.core import db as dbmod
from mystock2.ledger.events import post_dividend

from .ledger_helpers import ACCT
from .test_web_fixtures import NOW, RECEIVED, build_demo_db, buy, frozen_received, get_view, make_app
from .test_web_opsdata import LEAK_KEYS, LEAK_VALUES, SENT_PRED_HIGH, SENT_PRED_LOW, build_ops_db

UTC = timezone.utc
NAMES = {"US.NVDA": "合成英伟达", "HK.00700": "合成腾讯", "US.TSLA": "合成特斯拉"}
IMPORTED = "2026-03-10T00:00:00.000000Z"


# ------------------------------------------------------------------ 合成数据
def seed_names(db, names=None):
    w = dbmod.connect_writer(db, "ledger")
    for code, name in (names or NAMES).items():
        w.execute("INSERT INTO instrument_name(code, name, source, updated_at) VALUES (?,?,?,?)", (code, name, "synthetic", IMPORTED))
    w.close()


def seed_orders(db):
    w = dbmod.connect_writer(db, "ledger")
    rows = [
        # (order_id, code, side, type, status, price, qty, dealt_qty, dealt_avg, created, time_trust, source)
        ("o1", "US.NVDA", "BUY", "NORMAL", "FILLED_ALL", "100", "10", "10", "100", "2026-03-03T14:50:00.000000Z", "exact", "futu"),
        ("o2", "US.NVDA", "SELL", "NORMAL", "CANCELLED_ALL", "130", "5", "0", None, "2026-03-05T14:50:00.000000Z", "exact", "futu"),
        ("o3", "US.NVDA", "BUY", "MARKET", "FAILED", "0", "7", None, None, "2026-03-06T14:50:00.000000Z", "assumed_local_tz", "v1"),
        ("o4", "US.NVDA", "SELL", "NORMAL", "WEIRD_STATE", None, "3", "bad", None, "2026-03-07T14:50:00.000000Z", "exact", "futu"),
        ("o5", "HK.00700", "BUY", "NORMAL", "SUBMITTED", "300", "100", "0", None, "2026-03-09T02:00:00.000000Z", "exact", "futu"),
    ]
    for oid, code, side, typ, st, px, qty, dq, davg, created, trust, src in rows:
        w.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, dealt_qty, dealt_avg_price, created_at, "
                  "updated_at, time_trust, source, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ACCT, oid, code[:2], code, side, typ, st, px, qty, dq, davg, created, created, trust, src, IMPORTED))
    w.close()


def seed_profile(db, code="US.NVDA", **over):
    row = {"long_name": "Synthetic Chips Inc", "sector": "科技", "industry": "半导体", "exchange": "NMS", "currency": "USD", "market_cap_mm": "123456.789",
           "shares_mm": "2450", "trailing_pe": "55.5", "forward_pe": None, "price_to_book": "not-a-number", "trailing_eps": "2.1", "dividend_yield": "0.0012",
           "beta": "1.7", "week52_high": "180.5", "week52_low": "60.25", "lot_size": "1", "website": "https://synthetic.invalid", "source": "synthetic-profile",
           "as_of": "2026-03-09T00:00:00.000000Z", "updated_at": IMPORTED}
    row.update(over)
    w = dbmod.connect_writer(db, "market")
    cols = ["code", *row]
    w.execute(f"INSERT INTO instrument_profile({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [code, *row.values()])
    w.close()


def seed_flows(db, code="US.NVDA", days=25, source="synthetic-flow", start="2026-02-01"):
    from datetime import date, timedelta
    w = dbmod.connect_writer(db, "market")
    d0 = date.fromisoformat(start)
    for i in range(days):
        day = (d0 + timedelta(days=i)).isoformat()
        main = str(1000 + i) if i % 7 else None                      # 部分日子缺主力净流入
        w.execute("INSERT INTO capital_flow_daily(code, session_date, in_flow, main_in_flow, super_in_flow, big_in_flow, mid_in_flow, sml_in_flow, source, received_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (code, day, str(-500 + i), main, "10", "-20", "30", "-40", source, IMPORTED))
    w.close()


def seed_rebuilt(db, code="US.NVDA", *, with_forward_target=None):
    """事后重建预测：基线与 LGBM 各两条（取 as_of 最大者）；可选地再放一条同目标日的前向预测（用哨兵价位）。"""
    w = dbmod.connect_writer(db, "forecast")
    rows = [("naive_vol-v1", "2026-03-04", "2026-03-05", "90.1111", "99.9999"), ("naive_vol-v1", "2026-03-06", "2026-03-10", "95.5", "108.25"),
            ("lgbm-cqr-v1", "2026-03-06", "2026-03-10", "96", "107")]
    for i, (mv, asof, target, lo, hi) in enumerate(rows):
        gen = "2026-03-10T10:00:00.000000Z"
        w.execute("INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
                  "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"rb{code}{i}", code, asof, target, mv, "f1", "{}", "-0.05", "0.03", lo, hi, "0.02", 250, "[]", gen, gen, gen, "rebuilt", f"rbh{code}{i}", gen))
    if with_forward_target:
        w.execute("INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
                  "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"fw{code}", code, "2026-03-10", with_forward_target, "naive_vol-v1", "f1", "{}", "-0.098765", "0.054321", SENT_PRED_LOW, SENT_PRED_HIGH, "0.02", 250,
                   "[]", "2026-03-10T22:00:00.000000Z", "2026-03-10T22:00:00.000000Z", "2026-03-10T22:00:00.000000Z", "forward", f"fwh{code}", "2026-03-10T22:00:00.000000Z"))
    w.close()


def seed_dividends(db):
    w = dbmod.connect_writer(db, "ledger")
    with frozen_received():                                                    # 账本里没有 received_at 参数的写入也用固定的收到时间
        _seed_dividends(w)
    w.close()


def _seed_dividends(w):
    post_dividend(w, ACCT, "dv1", "HK.00700", "HKD", accrual_at="2026-03-04T02:00:00.000000Z", gross="100", payment_at="2026-03-05T02:00:00.000000Z", withholding_tax="10")
    post_dividend(w, ACCT, "dv2", "HK.00700", "HKD", accrual_at="2026-03-06T02:00:00.000000Z", gross="50", payment_at="2026-03-09T02:00:00.000000Z", cash_received="45")
    post_dividend(w, ACCT, "dv0", "HK.00700", "HKD", accrual_at="2026-02-20T02:00:00.000000Z", gross="999", payment_at="2026-02-25T02:00:00.000000Z", withholding_tax="1")   # 开账日前


def seed_many_fills(db, n=40):
    from .test_web_fixtures import fee
    w = dbmod.connect_writer(db, "ledger")
    for i in range(n):
        at = f"2026-03-0{(i % 5) + 3}T16:{i:02d}:00.000000Z"
        buy(w, f"m{i}", "US.TSLA", 1, 200 + i, at)
        fee(w, f"m{i}", "0.1", at)
    w.close()


def build_stock_db(tmp_path, **kw):
    db = build_demo_db(tmp_path, **kw)
    seed_names(db)
    seed_orders(db)
    seed_profile(db)
    seed_flows(db)
    seed_rebuilt(db)
    seed_dividends(db)
    return db


def stock(client, code="US.NVDA", **kw):
    st, body = get_view(client, "stock", code=code, **kw)
    assert st == 200, body
    return body


# ------------------------------------------------------------------ 中文名
def test_code_name_is_added_to_rows_with_a_code_and_only_when_known(tmp_path):
    db = build_stock_db(tmp_path)
    c = make_app(tmp_path, db).test_client()
    for vid in ("holdings", "trades", "pnl", "account_overview"):
        d = get_view(c, vid)[1]["data"]
        assert d["code_names"]["US.NVDA"] == NAMES["US.NVDA"], vid
    rows = get_view(c, "holdings")[1]["data"]["rows"]
    assert {r["code"]: r["code_name"] for r in rows} == {"US.NVDA": NAMES["US.NVDA"], "HK.00700": NAMES["HK.00700"]}
    pos = get_view(c, "account_overview")[1]["data"]["positions"]
    assert all(p["code_name"] == NAMES[p["code"]] for p in pos)
    sells = get_view(c, "pnl")[1]["data"]["sells"]
    assert sells and all(s["code_name"] == NAMES[s["code"]] for s in sells)


def test_missing_name_is_not_added_and_never_an_error(tmp_path):
    db = build_stock_db(tmp_path)
    w = dbmod.connect_writer(db, "ledger")
    w.execute("DELETE FROM instrument_name WHERE code='HK.00700'")
    w.close()
    c = make_app(tmp_path, db).test_client()
    rows = {r["code"]: r for r in get_view(c, "holdings")[1]["data"]["rows"]}
    assert rows["US.NVDA"]["code_name"] == NAMES["US.NVDA"] and "code_name" not in rows["HK.00700"]


def test_names_table_missing_or_empty_is_harmless(tmp_path):
    db = build_demo_db(tmp_path)                                              # instrument_name 为空
    c = make_app(tmp_path, db).test_client()
    body = get_view(c, "holdings")[1]
    assert body["status"] == "ok" and "code_names" not in body["data"] and all("code_name" not in r for r in body["data"]["rows"])
    w = sqlite3.connect(db)
    w.execute("DROP TABLE instrument_name")                                  # 旧库没有这张表
    w.commit()
    w.close()
    body = make_app(tmp_path, db).test_client().get("/api/v/holdings").get_json()
    assert body["status"] == "ok" and all("code_name" not in r for r in body["data"]["rows"])


def test_names_are_display_only_and_do_not_change_sealed_content(tmp_path):
    """名称不是密封内容：密封态的操作单响应除了名称外没有变化，且仍不含任何 AI 单内容。"""
    db = build_ops_db(tmp_path)
    seed_names(db)
    c = make_app(tmp_path, db).test_client()
    for vid in ("tickets", "holdings", "replay", "data_status", "scoreboard", "stock"):
        text = c.get(f"/api/v/{vid}", query_string={"code": "US.NVDA"}).get_data(as_text=True)
        for v in LEAK_VALUES:
            assert v not in text, (vid, v)
        for k in LEAK_KEYS:
            if vid == "tickets":                                              # 密封态：操作单视图里没有任何内容键（名称不改变这一点）
                assert k not in text, (vid, k)


def test_forecast_symbol_dropdown_gets_names(tmp_path):
    from .test_web_forecast import build_db as build_forecast_db
    db = build_forecast_db(tmp_path)
    seed_names(db, {"US.NVDA": "合成英伟达"})
    d = get_view(make_app(tmp_path, db).test_client(), "forecast")[1]["data"]
    assert d["code_names"] == {"US.NVDA": "合成英伟达"} and "US.NVDA" in d["codes"]
    assert any(r.get("code_name") == "合成英伟达" for r in d["compare"]["rows"] if r.get("code") == "US.NVDA")


# ------------------------------------------------------------------ 参数
@pytest.mark.parametrize("code", ["", "NVDA", "US.nvda", "HK.700", "HK.0070A", "US.", "FX.USDCNY", "US.NVDA ; DROP", "us.NVDA"])
def test_stock_requires_a_complete_us_or_hk_code(tmp_path, code):
    c = make_app(tmp_path).test_client()
    st, body = get_view(c, "stock", code=code)
    assert st == 400 and body["error"]["code"] == "bad_param", (code, body)


def test_stock_without_code_is_rejected_and_views_yaml_registers_it_hidden(tmp_path):
    c = make_app(tmp_path).test_client()
    assert get_view(c, "stock")[0] == 400
    app = make_app(tmp_path, views_config=__import__("pathlib").Path("config/views.yaml"))
    v = next(v for v in app.test_client().get("/api/views").get_json()["views"] if v["id"] == "stock")
    assert v["hidden"] is True and v["has_panel"] is True


# ------------------------------------------------------------------ 各块
def test_head_quote_and_52_week_range(tmp_path):
    db = build_stock_db(tmp_path)
    c = make_app(tmp_path, db).test_client()
    b = stock(c, "HK.00700")
    d = b["data"]
    assert d["code_name"] == NAMES["HK.00700"] and d["head"]["name"]["text"] == NAMES["HK.00700"] and d["currency"] == "HKD"
    q = d["quote"]
    assert q["close"]["text"] == "316.00 HKD" and q["session_date"] == "2026-03-10" and q["stale"] is False
    assert q["change"]["text"] == "+1.00 HKD" and q["change"]["dir"] == "up" and q["change_pct"]["text"] == "+0.32%" and q["change_pct"]["dir"] == "up"
    assert q["prev_close"]["text"] == "315.00 HKD"
    r = d["range52"]
    assert r["calc"]["high"]["text"] == "321.00 HKD" and r["calc"]["low"]["text"] == "299.00 HKD" and r["calc"]["partial"] is True and r["calc"]["days"] == 7
    assert r["profile"]["high"]["na"] is True and r["profile"]["source"] is None             # 该标的没有档案：来源值「不可用」，不影响自算

    n = stock(c, "US.NVDA")["data"]
    assert n["head"]["long_name"]["text"] == "Synthetic Chips Inc" and n["head"]["industry"]["text"] == "半导体" and n["head"]["exchange"]["text"] == "NMS"
    assert n["range52"]["profile"]["high"]["text"] == "180.50 USD" and n["range52"]["profile"]["source"] == "synthetic-profile"
    assert n["range52"]["profile"]["as_of"] == "2026-03-09T00:00:00.000000Z" and n["range52"]["calc"]["high"]["text"] == "111.00 USD"
    # 前一交易日缺行情：日涨跌「不可用」，不记 0、不拿更早的日子冒充
    assert n["quote"]["change"]["na"] is True and "缺行情" in n["quote"]["change"]["title"]


def test_stale_quote_is_flagged(tmp_path):
    db = build_stock_db(tmp_path)
    app = make_app(tmp_path, db, clock=lambda: datetime(2026, 3, 13, 6, 0, tzinfo=UTC))      # 03-12 已收盘但库里只有到 03-10 的行情
    q = stock(app.test_client(), "US.NVDA")["data"]["quote"]
    assert q["stale"] is True and q["close"]["tag"] == "陈旧 2026-03-10" and q["expected_session"] == "2026-03-12"


def test_daily_change_is_unavailable_across_a_split(tmp_path):
    db = build_stock_db(tmp_path)
    w = dbmod.connect_writer(db, "ledger")
    w.execute("INSERT INTO corporate_action(action_id, kind, market, code, ratio_num, ratio_den, effective_at, source, created_at) VALUES ('s1','SPLIT','HK','HK.00700',2,1,"
              "'2026-03-10T00:00:00.000000Z','synthetic','2026-03-10T00:00:00.000000Z')")
    w.close()
    q = stock(make_app(tmp_path, db).test_client(), "HK.00700")["data"]["quote"]
    assert q["change"]["na"] is True and "拆股" in q["change"]["title"]


def test_position_cells_are_identical_to_the_holdings_view(tmp_path):
    """复用而不是另写一套：同一标的的持仓/成本/浮动盈亏/集中度单元与持仓视图逐项相等。"""
    db = build_stock_db(tmp_path)
    c = make_app(tmp_path, db).test_client()
    rows = {r["code"]: r for r in get_view(c, "holdings")[1]["data"]["rows"]}
    for code in ("US.NVDA", "HK.00700"):
        pos, row = stock(c, code)["data"]["position"], rows[code]
        assert pos["held"] is True and pos["status"] == "ok"
        for k in ("qty", "broker_qty", "market_value", "weight", "broker_cost", "diluted_cost", "local_cost", "unrealized"):
            assert pos[k] == row[k], (code, k)
        assert pos["qty_match"] == row["qty_match"]
    n = stock(c, "US.NVDA")["data"]["position"]
    assert n["broker_cost"]["text"] == "85.00 USD" and n["diluted_cost"]["text"] == "-3.00 USD" and n["local_cost"]["tag"] == "估算"
    t = stock(c, "HK.00700")["data"]["position"]
    assert t["broker_cost"]["na"] is True and t["diluted_cost"]["na"] is True            # 快照没有成本：不用别的成本冒充


def test_not_held_symbol_still_answers(tmp_path):
    db = build_stock_db(tmp_path)
    p = stock(make_app(tmp_path, db).test_client(), "US.TSLA")["data"]
    assert p["position"]["status"] == "ok" and p["position"]["held"] is False and p["trades"]["total"] == 0 and p["trades"]["summary"] is None
    assert p["quote"]["status"] == "unavailable" and p["chart"]["status"] == "unavailable" and p["dividends"]["count"] == 0


def test_chart_has_closing_line_gaps_and_trade_marks_from_the_ledger(tmp_path):
    db = build_stock_db(tmp_path)
    ch = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["chart"]
    assert ch["from"] == "2026-03-02" and ch["to"] == "2026-03-10" and len(ch["dates"]) == len(ch["closes"]) == 7
    assert ch["closes"][ch["dates"].index("2026-03-04")] == "110"
    assert list(ch["gaps"]) == ["2026-03-09"] and ch["closes"][ch["dates"].index("2026-03-09")] is None            # 缺口：不插值、不记零
    marks = {(m["x"], m["side"]): m for m in ch["marks"]}
    assert set(marks) == {("2026-03-03", "BUY"), ("2026-03-04", "SELL")}
    assert marks[("2026-03-03", "BUY")]["y"] == "100" and "买入 10 股" in marks[("2026-03-03", "BUY")]["label"] and "USD" in marks[("2026-03-03", "BUY")]["label"]
    assert marks[("2026-03-04", "SELL")]["y"] == "110"
    assert ch["unplaced_fills"] == 1                                                    # 开账前（02-27）的卖出在图的范围之外：只计数


def test_trade_marks_merge_same_day_same_side_with_weighted_average_price(tmp_path):
    db = build_stock_db(tmp_path)
    w = dbmod.connect_writer(db, "ledger")
    buy(w, "e1", "HK.00700", 100, 300, "2026-03-04T02:00:00.000000Z")
    buy(w, "e2", "HK.00700", 300, 320, "2026-03-04T03:00:00.000000Z")
    w.close()
    m = next(m for m in stock(make_app(tmp_path, db).test_client(), "HK.00700")["data"]["chart"]["marks"] if m["x"] == "2026-03-04" and m["side"] == "BUY")
    assert m["y"] == "315" and m["n"] == 2 and "400 股" in m["label"] and "合并" in m["label"]


def test_trades_block_last_30_with_fees_and_realized_pnl_in_the_pnl_view_basis(tmp_path):
    db = build_stock_db(tmp_path)
    seed_many_fills(db, 40)
    c = make_app(tmp_path, db).test_client()
    t = stock(c, "US.TSLA")["data"]["trades"]
    assert t["total"] == 40 and t["shown"] == 30 and len(t["rows"]) == 30
    ats = [r["event_at"] for r in t["rows"]]
    assert ats == sorted(ats, reverse=True) and all(r["fee"]["text"].endswith("USD") for r in t["rows"])
    n = stock(c, "US.NVDA")["data"]["trades"]
    rows = {r["deal_id"]: r for r in n["rows"]}
    sells = {s["ref"]: s for s in get_view(c, "pnl", code="US.NVDA")[1]["data"]["sells"]}
    assert rows["d2"]["realized"] == sells["d2"]["realized"] and rows["d2"]["quality_text"] == "估算" and rows["d2"]["realized"]["tag"] == "估算"
    assert rows["d0"]["pre_opening"] is True and rows["d0"]["realized"]["na"] is True and rows["d0"]["quality_text"] == "开账前"      # 开账前：不产生盈亏
    assert rows["d1"]["realized"] == {"text": "—"} and rows["d1"]["fee"]["title"] == "佣金 1.00 USD；平台费 0.50 USD"
    pnl_row = next(r for r in get_view(c, "pnl")[1]["data"]["by_code"] if r["code"] == "US.NVDA")
    for k in ("realized_exact", "realized_estimated", "unavailable_qty", "fees_total"):
        assert n["summary"][k] == pnl_row[k], k
    # 无成本证据的卖出：不可用，不给数
    h = stock(c, "HK.00700")["data"]["trades"]
    d4 = next(r for r in h["rows"] if r["deal_id"] == "d4")
    ps = next(x for x in get_view(c, "pnl", code="HK.00700")[1]["data"]["sells"] if x["ref"] == "d4")
    assert d4["realized"] == ps["realized"] and d4["quality_text"] == ps["quality_text"] and ps["quality_text"] != "精确"      # 部分来自无成本证据的开账持仓：口径与盈亏视图一致


def test_dividends_and_withholding_tax_from_ledger_events(tmp_path):
    db = build_stock_db(tmp_path)
    b = stock(make_app(tmp_path, db).test_client(), "HK.00700")["data"]["dividends"]
    assert b["count"] == 2 and b["skipped_pre_opening"] == 1                              # 开账日前那笔只作描述，不计入
    tot = b["totals"][0]
    assert tot["cash"]["text"] == "145.00 HKD"                                           # 100（税前总额）+ 45（只知净额）
    assert tot["tax"]["text"] == "10.00 HKD" and tot["shortfall"]["text"] == "5.00 HKD" and tot["net"]["text"] == "135.00 HKD"
    first = b["rows"][0]                                                                  # 最新在前：dv2（只知净额）
    assert first["cash"]["text"] == "45.00 HKD" and first["tax"]["text"] == "无" and first["shortfall"]["text"] == "5.00 HKD"
    none = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["dividends"]
    assert none["count"] == 0 and none["totals"] == []                                    # 无记录，不是 0


def test_orders_block_shows_status_for_cancelled_and_failed_and_is_an_intent(tmp_path):
    db = build_stock_db(tmp_path)
    o = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["orders"]
    assert o["total"] == 4 and o["shown"] == 4
    by = {r["status"]["raw"]: r for r in o["rows"]}
    assert by["FILLED_ALL"]["status"]["text"] == "全部成交" and by["CANCELLED_ALL"]["status"]["text"] == "已撤单（未成交）" and by["FAILED"]["status"]["text"] == "失败"
    assert by["WEIRD_STATE"]["status"]["text"] == "WEIRD_STATE"                           # 未识别状态原样显示，不猜
    assert by["FAILED"]["assumed_tz"] is True and by["FILLED_ALL"]["assumed_tz"] is False
    assert by["FAILED"]["price"]["na"] is True                                            # 价格 0（市价单）不是价格
    assert by["WEIRD_STATE"]["price"]["na"] is True and by["WEIRD_STATE"]["dealt_qty"]["na"] is True      # 缺失/无法解析：不可用
    assert by["CANCELLED_ALL"]["price"]["text"] == "130.00 USD" and by["CANCELLED_ALL"]["dealt_avg_price"]["na"] is True
    assert [r["created_at"] for r in o["rows"]] == sorted((r["created_at"] for r in o["rows"]), reverse=True)
    assert {x["raw"]: x["count"] for x in o["by_status"]}["CANCELLED_ALL"] == 1


def test_orders_do_not_enter_position_or_pnl(tmp_path):
    db = build_stock_db(tmp_path)
    c = make_app(tmp_path, db).test_client()
    before = stock(c, "US.NVDA")["data"]
    w = dbmod.connect_writer(db, "ledger")
    w.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, dealt_qty, dealt_avg_price, created_at, time_trust, source, first_seen_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ACCT, "big", "US", "US.NVDA", "BUY", "NORMAL", "FILLED_ALL", "1", "99999", "99999", "1", "2026-03-09T14:00:00.000000Z", "exact", "futu", IMPORTED))
    w.close()
    after = stock(c, "US.NVDA")["data"]
    assert after["orders"]["total"] == before["orders"]["total"] + 1
    for k in ("position", "trades", "chart"):
        assert after[k] == before[k], k


def test_orders_limit_30(tmp_path):
    db = build_stock_db(tmp_path)
    w = dbmod.connect_writer(db, "ledger")
    for i in range(35):
        w.execute("INSERT INTO broker_order(account_id, order_id, market, code, side, order_type, status, price, qty, created_at, time_trust, source, first_seen_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (ACCT, f"x{i}", "HK", "HK.00700", "BUY", "NORMAL", "CANCELLED_ALL", "300", "100", f"2026-02-{i % 27 + 1:02d}T02:00:00.000000Z", "exact", "futu", IMPORTED))
    w.close()
    o = stock(make_app(tmp_path, db).test_client(), "HK.00700")["data"]["orders"]
    assert o["total"] == 36 and o["shown"] == 30


def test_capital_flow_last_20_days_with_ccy_source_and_missing_fields(tmp_path):
    db = build_stock_db(tmp_path)
    f = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["flows"]
    assert f["days"] == 20 and len(f["rows"]) == 20 and f["latest"] == "2026-02-25" and f["ccy"] == "USD"
    assert [r["date"] for r in f["rows"]] == sorted((r["date"] for r in f["rows"]), reverse=True)
    r0 = f["rows"][0]
    assert r0["source"] == "synthetic-flow" and r0["main_in_flow"]["text"] == "+1,024.00 USD" and r0["main_in_flow"]["dir"] == "up" and r0["in_flow"]["dir"] == "down"
    miss = next(r for r in f["rows"] if r["main_in_flow"].get("na"))
    assert miss["main_in_flow"]["v"] is None and miss["main_in_flow"]["text"] == "不可用"      # 缺失：不记 0
    t = f["totals"][0]
    assert t["main_days"] < t["days"] == 20 and t["main_in_flow"]["ccy"] == "USD"
    assert f["stale"] is True                                                             # 最新记录日早于应有的最近收盘日


def test_capital_flow_two_sources_are_listed_not_summed(tmp_path):
    db = build_stock_db(tmp_path)
    seed_flows(db, days=3, source="other-source", start="2026-02-23")
    f = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["flows"]
    assert {t["source"] for t in f["totals"]} == {"synthetic-flow", "other-source"}


def test_forecast_shows_only_the_latest_rebuilt_row_per_model_and_is_labelled(tmp_path):
    db = build_stock_db(tmp_path)
    b = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["forecast"]
    assert b["total_rebuilt"] == 3 and [r["model"] for r in b["rows"]] == ["基线", "LightGBM+CQR"]
    base = b["rows"][0]
    assert base["as_of"] == "2026-03-06" and base["target"] == "2026-03-10" and base["low"]["text"] == "95.50 USD" and base["high"]["text"] == "108.25 USD"
    assert base["tag"] == "事后重建、非前向证据"
    assert base["actual"]["low"]["text"] == "105.00 USD" and base["actual"]["high"]["text"] == "107.00 USD"      # 目标日已结算：给出实际
    assert base["rel_low"]["dir"] == "down" and base["rel_high"]["dir"] == "up"


def test_forecast_never_includes_forward_predictions_and_shows_rebuilt_levels_even_for_an_open_target(tmp_path):
    db = build_stock_db(tmp_path)
    seed_rebuilt(db, "HK.00700", with_forward_target="2026-03-11")
    c = make_app(tmp_path, db).test_client()
    text = c.get("/api/v/stock", query_string={"code": "HK.00700"}).get_data(as_text=True)
    assert SENT_PRED_LOW not in text and SENT_PRED_HIGH not in text and "-0.098765" not in text            # 前向预测的数值根本不读取
    rows = json.loads(text)["data"]["forecast"]["rows"]
    assert rows and all("sealed" not in r and r["low"]["text"] for r in rows)                                               # 重建行的目标日是 03-10（已结算），不受前向影响
    # 重建预测与前向预测同一个未结束的目标日：不再密封（负责人 2026-10-06 决定），显示重建预测的价位；前向预测的数值仍不读取
    w = dbmod.connect_writer(db, "forecast")
    gen = "2026-03-10T10:30:00.000000Z"
    w.execute("INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, low_price, "
              "high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("rb9", "HK.00700", "2026-03-10", "2026-03-11", "lgbm-cqr-v1", "f1", "{}", "-0.05", "0.03", "271.1234", "301.5678", "0.02", 250, "[]", gen, gen, gen, "rebuilt", "rbh9", gen))
    w.close()
    text = c.get("/api/v/stock", query_string={"code": "HK.00700"}).get_data(as_text=True)
    lg = next(r for r in json.loads(text)["data"]["forecast"]["rows"] if r["model"] == "LightGBM+CQR")
    assert "sealed" not in lg and lg["low"]["text"] == "271.12 HKD" and lg["high"]["text"] == "301.57 HKD"
    assert SENT_PRED_LOW not in text and SENT_PRED_HIGH not in text


def test_stock_view_has_no_tickets_or_ai_order_fields_at_all(tmp_path):
    db = build_ops_db(tmp_path, reveal_target=True)
    seed_names(db)
    seed_rebuilt(db, "US.NVDA", with_forward_target="2026-03-11")
    text = make_app(tmp_path, db).test_client().get("/api/v/stock", query_string={"code": "US.NVDA"}).get_data(as_text=True)
    for v in LEAK_VALUES:
        assert v not in text, v
    for k in ('"ticket', '"action"', '"limit_price"', '"reasons"', '"frozen_hash"', '"reserved_cash"'):
        assert k not in text, k


def test_profile_metrics_source_as_of_and_missing_values(tmp_path):
    db = build_stock_db(tmp_path)
    p = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]["profile"]
    items = {i["key"]: i["cell"] for i in p["items"]}
    assert items["market_cap_mm"]["text"] == "123,456.79 百万 USD" and items["trailing_pe"]["text"] == "55.5" and items["beta"]["text"] == "1.7"
    assert items["forward_pe"]["na"] is True and items["forward_pe"]["text"] == "不可用"           # 缺失：不记 0
    assert items["price_to_book"]["na"] is True and "无法解析" in items["price_to_book"]["title"]
    assert p["source"] == "synthetic-profile" and p["as_of"] == "2026-03-09T00:00:00.000000Z" and p["ccy"] == "USD"
    assert stock(make_app(tmp_path, db).test_client(), "HK.00700")["data"]["profile"]["status"] == "unavailable"


# ------------------------------------------------------------------ 块相互独立
def test_each_block_fails_independently(tmp_path):
    db = build_stock_db(tmp_path, quotes=False)
    c = make_app(tmp_path, db).test_client()
    d = stock(c, "US.NVDA")["data"]
    assert d["quote"]["status"] == "unavailable" and d["chart"]["status"] == "unavailable"
    assert d["position"]["status"] == "ok" and d["position"]["market_value"]["na"] is True      # 缺行情：市值不可用，不记 0
    assert d["position"]["unrealized"]["na"] is True and d["trades"]["status"] == "ok" and d["orders"]["status"] == "ok" and d["profile"]["status"] == "ok"
    assert d["range52"]["calc"] is None and d["range52"]["profile"]["high"]["text"] == "180.50 USD"
    # 删掉订单表、没有账户：只影响各自的块
    w = sqlite3.connect(db)
    w.execute("DROP TABLE broker_order")
    w.commit()
    w.close()
    d = stock(make_app(tmp_path, db).test_client(), "US.NVDA")["data"]
    assert d["orders"]["status"] == "unavailable" and "表不存在" in d["orders"]["reason"] and d["position"]["status"] == "ok"


def test_no_account_only_blocks_ledger_parts(tmp_path):
    p = tmp_path / "m.db"
    dbmod.migrate(p)
    mk = dbmod.connect_writer(p, "market")
    from .test_web_fixtures import NVDA_CLOSES, put_closes
    put_closes(mk, "US.NVDA", NVDA_CLOSES)
    mk.close()
    d = stock(make_app(tmp_path, p).test_client(), "US.NVDA")["data"]
    assert d["quote"]["status"] == "ok" and d["range52"]["status"] == "ok"
    assert d["position"]["status"] == "unavailable" and "没有账户" in d["position"]["reason"]
    assert d["trades"]["status"] == "unavailable" and d["dividends"]["status"] == "unavailable"
    assert d["chart"]["status"] == "ok" and d["chart"]["marks"] == [] and "买卖标记不可用" in d["chart"]["mark_note"]      # 走势照常，只是没有标记
    assert d["orders"]["status"] == "ok" and d["orders"]["total"] == 0


def test_header_freshness_lists_quote_and_ledger_sources(tmp_path):
    db = build_stock_db(tmp_path)
    h = make_app(tmp_path, db).test_client().get("/api/v/stock", query_string={"code": "US.NVDA"}).get_json()["header"]
    assert {s["name"] for s in h["sources"]} == {"日线行情（未复权收盘）", "账本事件"} and h["staleness"]["label"] == "新鲜"


def test_stock_response_is_deterministic_and_uses_the_injected_clock(tmp_path):
    db = build_stock_db(tmp_path)
    c = make_app(tmp_path, db).test_client()
    a = c.get("/api/v/stock", query_string={"code": "US.NVDA"}).get_data()
    assert c.get("/api/v/stock", query_string={"code": "US.NVDA"}).get_data() == a
    assert RECEIVED < NOW
