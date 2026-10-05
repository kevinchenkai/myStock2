"""预测效果视图（forecast）：模型对比、覆盖率/pinball/改善、V1 门槛的描述性判定、来源（rebuilt/forward）、图表数据、
最新预测的密封、新鲜度与缺失处理。全部使用合成数据（合成测试值；不含任何真实数值）。"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from mystock2.core import calendars as cal
from mystock2.core import db as dbmod
from mystock2.market.bars import DailyBar, put_daily
from mystock2.web import registry

from .test_web_fixtures import NOW, RECEIVED, get_view, make_app
from .test_web_opsdata import add_reveal

pytestmark = pytest.mark.filterwarnings("ignore")

UTC = timezone.utc
GEN = "2026-03-10T22:00:00.000000Z"
# 哨兵值：只出现在「目标日尚未结束」的预测里；密封时响应里绝不能有它们
SENT = {"low_price": "77.1234", "high_price": "123.4321", "y_low": "-0.077777", "y_high": "0.088888"}


def sessions(market, start, n):
    days = cal.session_days(market, start, start + timedelta(days=n * 2 + 10))
    return days[:n]


SESS = sessions("US", date(2026, 1, 5), 41)    # 41 个美股交易日：40 个「T→T+1」样本
NEXT = cal.next_session("US", SESS[-1])         # 最后一根之后的下一交易日（没有行情＝目标日未结束）


def put_flat_quotes(db, code, sessions, *, close="100", high="103", low="98", adj=None, quality="ok", version_bump=False):
    """行情：常数价，实际标签恒为 y_low=-0.02、y_high=+0.03（复权因子为 1）。"""
    mk = dbmod.connect_writer(db, "market")
    put_daily(mk, [DailyBar(code, d, close, high, low, close, adj or close, "1000") for d in sessions], source="synthetic", received_at=RECEIVED, quality=quality)
    mk.close()


def insert_pred(fw, n, code, as_of, target, mv, y_low, y_high, *, tag="rebuilt", gen=GEN, alphas=(0.1, 0.9), prices=None, params=True):
    close = D("100")
    lp = prices[0] if prices else str(close * (1 + D(y_low)))
    hp = prices[1] if prices else str(close * (1 + D(y_high)))
    pj = json.dumps({"alpha_low": alphas[0], "alpha_high": alphas[1]}) if params else "{}"
    fw.execute("INSERT INTO prediction_version(prediction_id, code, as_of_session, target_session, model_version, feature_version, params_json, y_low, y_high, "
               "low_price, high_price, scale, n_train, input_snapshot_ids, input_cutoff_at, generated_at, available_at, source_tag, content_hash, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"p{n}", code, as_of.isoformat(), target.isoformat(), mv, "f1", pj, y_low, y_high, lp, hp, "0.02", 250, "[]",
                f"{as_of.isoformat()}T21:00:00.000000Z", gen, gen, tag, f"ch{n}", gen))


class Seq:
    n = 0


def fill_preds(db, code, *, base=None, lgbm=None, count=40, tag="rebuilt", base_mv="naive-vol-v1", lgbm_mv="lgbm-cqr-v1"):
    """base/lgbm：函数 i -> (y_low, y_high) 字符串；None 表示该模型没有预测。"""
    fw = dbmod.connect_writer(db, "forecast")
    for i in range(count):
        for mv, fn in ((base_mv, base), (lgbm_mv, lgbm)):
            if fn is None:
                continue
            Seq.n += 1
            yl, yh = fn(i)
            insert_pred(fw, Seq.n, code, SESS[i], SESS[i + 1], mv, yl, yh, tag=tag)
    fw.close()


# 基线：前 4 条低点预测过高（实际 -0.02 跌破 -0.01）、前 2 条高点预测过低（实际 +0.03 突破 +0.02）；其余宽松
BASE_A = lambda i: ("-0.01" if i < 4 else "-0.03", "0.02" if i < 2 else "0.05")  # noqa: E731
LGBM_A = lambda i: ("-0.025", "0.04")  # noqa: E731
BASE_B = lambda i: ("-0.025", "0.04")  # noqa: E731
LGBM_B = lambda i: ("-0.03", "0.05")  # noqa: E731


def build_db(tmp_path, *, codes=("US.NVDA", "US.TSLA", "US.PDD"), preds=True):
    db = tmp_path / "fc.db"
    dbmod.migrate(db)
    for c in codes:
        put_flat_quotes(db, c, SESS)
    if preds:
        if "US.NVDA" in codes:
            fill_preds(db, "US.NVDA", base=BASE_A, lgbm=LGBM_A)
        if "US.TSLA" in codes:
            fill_preds(db, "US.TSLA", base=BASE_B, lgbm=LGBM_B)
        if "US.PDD" in codes:
            fill_preds(db, "US.PDD", base=BASE_A, lgbm=LGBM_A, count=10)         # 样本不足 30
    return db


def view(tmp_path, db, **params):
    c = make_app(tmp_path, db).test_client()
    code, body = get_view(c, "forecast", **params)
    assert code == 200, body
    return body


def row(body, code):
    d = body["data"]
    if code == "summary":
        return d["compare"]["summary"]
    return next(r for r in d["compare"]["rows"] if r["code"] == code)


def v(cell):
    return D(cell["v"])


# ====================================================================== 数值口径（手算对照）
def test_coverage_width_pinball_and_improvement_match_hand_computation(tmp_path):
    body = view(tmp_path, build_db(tmp_path))
    assert body["status"] == "ok"
    r = row(body, "US.NVDA")
    assert r["n"] == 40 and r["enough"]
    b, g = r["baseline"], r["lgbm"]
    assert v(b["cover_low"]) == D("0.1") and b["cover_low"]["text"] == "10.0%"           # 4/40 跌破低点
    assert v(b["cover_high"]) == D("0.05") and b["cover_high"]["text"] == "5.0%"         # 2/40 突破高点
    assert v(b["width"]) == D("0.0765") and b["width"]["text"] == "7.65%"       # (0.0485 − (−0.028))
    # 低侧 4×0.009+36×0.001=0.072；高侧 2×0.009+38×0.002=0.094；总损失 0.166/40=0.00415
    assert v(b["pinball"]) == D("0.00415") and b["pinball"]["text"] == "0.4150%"
    assert v(g["cover_low"]) == 0 and g["cover_low"]["text"] == "0.0%"                    # 真实的 0（样本充足），不是缺失
    assert v(g["width"]) == D("0.065") and v(g["pinball"]) == D("0.0015")                 # 0.0005 + 0.001
    assert D(r["improvement"]["v"]).quantize(D("0.0001")) == D("0.6386") and r["improvement"]["text"] == "+63.9%"
    t = row(body, "US.TSLA")
    assert v(t["baseline"]["pinball"]) == D("0.0015") and v(t["lgbm"]["pinball"]) == D("0.003")
    assert v(t["improvement"]) == D("-1") and t["improvement"]["text"] == "-100.0%"        # 候选更差：负改善，如实显示


def test_nominal_targets_come_from_prediction_params(tmp_path):
    m = {x["role"]: x for x in view(tmp_path, build_db(tmp_path))["data"]["models"]}
    assert m["baseline"]["version"] == "naive-vol-v1" and m["lgbm"]["version"] == "lgbm-cqr-v1"
    assert m["baseline"]["target_low"]["text"] == "10.0%" and m["baseline"]["target_high"]["text"] == "10.0%"     # α_low=0.1；1−α_high=0.1
    assert m["baseline"]["predictions"] == 90 and m["baseline"]["scored"] == 90 and m["baseline"]["pending"] == 0


def test_insufficient_samples_show_buzu_not_zero(tmp_path):
    body = view(tmp_path, build_db(tmp_path))
    hk = row(body, "US.PDD")
    assert hk["n"] == 10 and not hk["enough"]
    for model in ("baseline", "lgbm"):
        for k in ("cover_low", "cover_high", "width", "pinball"):
            c = hk[model][k]
            assert c["text"] == "不足" and c["v"] is None and c["na"]
    assert hk["improvement"]["text"] == "不足" and hk["improvement"]["v"] is None
    assert "US.PDD" in body["data"]["compare"]["gate"]["excluded"]


def test_summary_pools_enough_names_and_gate_is_descriptive(tmp_path):
    body = view(tmp_path, build_db(tmp_path))
    s, gate = row(body, "summary"), body["data"]["compare"]["gate"]
    assert s["n"] == 80 and "2 个标的" in s["code"]                                   # US.PDD 样本不足，不入汇总
    assert v(s["baseline"]["cover_low"]) == D("0.05")                                       # (4+0)/80，按样本合并
    mean = (D("0.63855421686746987951807228915") + D(-1)) / 2
    assert D(s["improvement"]["v"]).quantize(D("0.0001")) == mean.quantize(D("0.0001"))   # 等权平均改善（门槛口径）
    assert gate["available"] and gate["pass"] is False and gate["n_names"] == 2 and gate["names_improved"] == 1 and gate["names_needed"] == 2
    assert "未达到" in gate["text"] and "1/2" in gate["text"] and "-18.1%" in gate["text"]
    assert "不触发任何晋级" in gate["disclaimer"]


def test_gate_passes_numerically_when_conditions_hold_but_stays_descriptive(tmp_path):
    body = view(tmp_path, build_db(tmp_path, codes=("US.NVDA",)))
    g = body["data"]["compare"]["gate"]
    assert g["pass"] is True and g["n_names"] == 1 and g["names_needed"] == 1            # ⌈2·1/3⌉ = 1
    assert g["text"].startswith("达到 V1 门槛的数值条件") and "63.9%" in g["text"]
    assert "不触发任何晋级" in g["disclaimer"]
    assert any("rebuilt" in w and "不能当作晋级证据" in w for w in body["data"]["warnings"])


def test_gate_uses_ceil_two_thirds_of_names(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA", "US.TSLA"))
    put_flat_quotes(db, "US.AMD", SESS)
    fill_preds(db, "US.AMD", base=BASE_A, lgbm=LGBM_A)                                      # 第三个标的，也改善
    g = view(tmp_path, db)["data"]["compare"]["gate"]
    assert g["n_names"] == 3 and g["names_improved"] == 2 and g["names_needed"] == 2        # ⌈2·3/3⌉ = 2
    assert D(g["mean_improvement"]).quantize(D("0.0001")) == D("0.0924")                    # (0.6386 + 0.6386 − 1) / 3
    assert g["pass"] is True and "达到" in g["text"] and "2/3" in g["text"]


# ====================================================================== 来源：rebuilt / forward 不混算
def test_provenance_counts_and_forward_zero_warning(tmp_path):
    d = view(tmp_path, build_db(tmp_path))["data"]
    assert d["forward_count"] == 0 and d["rebuilt_count"] == 80 + 80 + 20 and d["source"] == "rebuilt"
    prov = {p["tag"]: p for p in d["provenance"]}
    assert prov["rebuilt"]["first_as_of"] == SESS[0].isoformat() and prov["rebuilt"]["last_as_of"] == SESS[39].isoformat() and prov["rebuilt"]["codes"] == 3
    assert prov["forward"]["count"] == 0 and prov["forward"]["first_as_of"] is None
    assert any("前向" in w and "为 0" in w for w in d["warnings"])
    assert "不构成投资建议" in d["banner"] and "不是成交保证" in d["banner"]


def test_forward_and_rebuilt_are_never_mixed(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    fill_preds(db, "US.NVDA", base=lambda i: ("-0.5", "0.5"), lgbm=lambda i: ("-0.5", "0.5"), count=35, tag="forward")        # 极宽的前向预测
    d_r = view(tmp_path, db)["data"]
    d_f = view(tmp_path, db, source="forward")["data"]
    assert d_r["forward_count"] == 70 and d_r["rebuilt_count"] == 80
    assert row({"data": d_r}, "US.NVDA")["n"] == 40                                          # rebuilt：只算 rebuilt 的 40 条
    assert row({"data": d_f}, "US.NVDA")["n"] == 35                                          # forward：只算 forward 的 35 条
    assert v(row({"data": d_f}, "US.NVDA")["baseline"]["width"]) == D("1")
    assert d_f["source"] == "forward" and "forward" in d_f["source_label"]
    assert not any("为 0" in w and "前向" in w for w in d_f["warnings"])


def test_forward_source_with_no_forward_rows_is_empty_not_zero(tmp_path):
    d = view(tmp_path, build_db(tmp_path), source="forward")["data"]
    assert d["compare"]["rows"] == [] and d["compare"]["summary"]["n"] == 0 and d["compare"]["summary"]["enough"] is False
    assert d["compare"]["summary"]["baseline"]["cover_low"]["na"] and d["chart"] is None
    assert d["compare"]["gate"]["available"] is False


# ====================================================================== 缺失与口径细节
def test_single_model_gives_unavailable_not_zero_and_no_gate(tmp_path):
    db = tmp_path / "one.db"
    dbmod.migrate(db)
    put_flat_quotes(db, "US.NVDA", SESS)
    fill_preds(db, "US.NVDA", base=BASE_A, lgbm=None)
    d = view(tmp_path, db)["data"]
    r = row({"data": d}, "US.NVDA")
    assert v(r["baseline"]["cover_low"]) == D("0.1")
    assert r["lgbm"]["cover_low"]["na"] and r["lgbm"]["cover_low"]["text"] == "不可用" and r["lgbm"]["cover_low"]["v"] is None
    assert r["improvement"]["na"] and r["improvement"]["v"] is None
    assert d["compare"]["gate"]["available"] is False and "不可用" in d["compare"]["gate"]["text"]
    assert d["models"][1]["version"] == "不可用" and d["models"][1]["predictions"] == 0


def test_no_predictions_is_a_business_state(tmp_path):
    db = build_db(tmp_path, preds=False)
    c = make_app(tmp_path, db).test_client()
    code, body = get_view(c, "forecast")
    assert code == 200 and body["status"] == "unavailable" and body["error"]["code"] == "no_predictions"


def test_predictions_without_actuals_are_pending_not_scored(tmp_path):
    db = tmp_path / "p.db"
    dbmod.migrate(db)
    put_flat_quotes(db, "US.NVDA", SESS[:20])                                 # 只有前 20 日行情
    fill_preds(db, "US.NVDA", base=BASE_A, lgbm=LGBM_A)                       # 40 条预测，后面的目标日没有行情
    d = view(tmp_path, db)["data"]
    m = d["models"][0]
    assert m["predictions"] == 40 and m["scored"] == 19 and m["pending"] == 21
    r = row({"data": d}, "US.NVDA")
    assert r["n"] == 19 and not r["enough"] and r["baseline"]["cover_low"]["text"] == "不足"


def test_labels_use_adjusted_prices_so_a_split_is_not_a_false_breach(tmp_path):
    db = tmp_path / "s.db"
    dbmod.migrate(db)
    put_flat_quotes(db, "US.NVDA", SESS[:-1])
    mk = dbmod.connect_writer(db, "market")
    put_daily(mk, [DailyBar("US.NVDA", SESS[-1], "50", "51.5", "49", "50", "100", "1000")], source="synthetic", received_at=RECEIVED)   # 1:2 拆股：原始价减半，复权价不变
    mk.close()
    fill_preds(db, "US.NVDA", base=lambda i: ("-0.03", "0.05"), lgbm=lambda i: ("-0.03", "0.05"))
    d = view(tmp_path, db, symbol="US.NVDA")["data"]
    assert d["models"][0]["scored"] == 40
    pts = d["chart"]["models"][0]["points"]
    assert pts[-1]["breach_low"] is False and pts[-1]["breach_high"] is False             # 复权后实际低点 −2%，在 −3% 之内；若用原始价会误判为 −51%


def test_only_the_highest_ok_version_of_a_quote_is_used(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    mk = dbmod.connect_writer(db, "market")
    # 更正版（version 2）把某日低点改为 90：实际低点 −0.1，应突破预测低点 −0.03；partial 版本不参与
    put_daily(mk, [DailyBar("US.NVDA", SESS[10], "100", "103", "90", "100", "100", "1000")], source="synthetic", received_at=RECEIVED)
    put_daily(mk, [DailyBar("US.NVDA", SESS[11], "100", "103", "10", "100", "100", "1000")], source="synthetic", received_at=RECEIVED, quality="partial")
    mk.close()
    pts = view(tmp_path, db, symbol="US.NVDA", window="250")["data"]["chart"]
    i = pts["dates"].index(SESS[10].isoformat())
    assert pts["actual"][i]["low"] == "90"
    j = pts["dates"].index(SESS[11].isoformat())
    assert pts["actual"][j]["low"] == "98"                                                  # partial 不当实际值


def test_mixed_alpha_rows_are_excluded_and_noted(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    fw = dbmod.connect_writer(db, "forecast")
    insert_pred(fw, 9001, "US.NVDA", SESS[0], SESS[1], "naive-vol-v1", "-0.03", "0.05", gen="2026-03-10T23:00:00.000000Z", alphas=(0.05, 0.95))   # 同键新版本，分位参数与多数不一致
    fw.close()
    d = view(tmp_path, db)["data"]
    assert d["models"][0]["unscorable"] == 1 and d["models"][0]["scored"] == 39
    assert any("未计入" in w for w in d["warnings"])


def test_only_latest_version_per_family_is_used(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    fw = dbmod.connect_writer(db, "forecast")
    insert_pred(fw, 9100, "US.NVDA", SESS[0], SESS[1], "lgbm-cqr-v0", "-0.5", "0.5", gen="2026-03-01T00:00:00.000000Z")
    insert_pred(fw, 9101, "US.NVDA", SESS[0], SESS[1], "mystery-model", "-0.5", "0.5")
    fw.close()
    d = view(tmp_path, db)["data"]
    assert d["models"][1]["version"] == "lgbm-cqr-v1"
    assert any("lgbm-cqr-v0" in w and "mystery-model" in w for w in d["warnings"])


# ====================================================================== 图表
def test_chart_window_points_gaps_and_breach_flags(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    c = view(tmp_path, db, window="60")["data"]["chart"]
    assert c["code"] == "US.NVDA" and c["currency"] == "USD" and len(c["dates"]) == 41                 # 窗口 60 > 41 个交易日：全部
    c20 = make_app(tmp_path, db).test_client().get("/api/v/forecast?window=60").get_json()["data"]["chart"]
    assert c20["dates"][0] == SESS[0].isoformat() and c20["dates"][-1] == SESS[-1].isoformat()
    base = next(m for m in c["models"] if m["role"] == "baseline")["points"]
    assert base[0] is None                                                                           # 第一个交易日没有以它为目标日的预测：不画，不插值
    assert len(base) == 41 and base[1]["breach_low"] is True and base[1]["breach_high"] is True      # as_of=SESS[0]（i=0）：低点、高点都被突破
    assert base[5]["breach_low"] is False and base[5]["breach_high"] is False                        # i=4
    assert D(base[1]["low"]) == D("99") and D(base[1]["high"]) == D("102")
    assert c["actual"][3]["low"] == "98" and c["actual"][3]["high"] == "103"
    assert all(a["dir"] in ("flat", None) for a in c["actual"])                # 价格没变：平，不着色


def test_chart_window_limits_the_number_of_days(tmp_path):
    db = tmp_path / "long.db"
    dbmod.migrate(db)
    sess = sessions("US", date(2025, 6, 2), 130)
    put_flat_quotes(db, "US.NVDA", sess)
    fw = dbmod.connect_writer(db, "forecast")
    for i in range(129):
        insert_pred(fw, i, "US.NVDA", sess[i], sess[i + 1], "naive-vol-v1", "-0.03", "0.05")
    fw.close()
    c = make_app(tmp_path, db, clock=lambda: datetime(2026, 3, 11, 6, 0, tzinfo=UTC)).test_client()
    assert len(c.get("/api/v/forecast?window=60").get_json()["data"]["chart"]["dates"]) == 60
    assert len(c.get("/api/v/forecast").get_json()["data"]["chart"]["dates"]) == 120                  # 默认 120
    assert len(c.get("/api/v/forecast?window=250").get_json()["data"]["chart"]["dates"]) == 130
    assert c.get("/api/v/forecast?window=90").status_code == 400                                       # 只允许 60/120/250
    assert c.get("/api/v/forecast?source=other").status_code == 400


def test_chart_price_direction_uses_close_vs_previous_close(tmp_path):
    db = tmp_path / "dir.db"
    dbmod.migrate(db)
    mk = dbmod.connect_writer(db, "market")
    closes = ["100", "102", "101", "101"]
    put_daily(mk, [DailyBar("US.NVDA", SESS[i], c, str(D(c) + 1), str(D(c) - 1), c, c, "1") for i, c in enumerate(closes)], source="s", received_at=RECEIVED)
    mk.close()
    fw = dbmod.connect_writer(db, "forecast")
    for i in range(3):
        insert_pred(fw, i, "US.NVDA", SESS[i], SESS[i + 1], "naive-vol-v1", "-0.03", "0.05")
    fw.close()
    c = view(tmp_path, db)["data"]["chart"]
    assert [a["dir"] for a in c["actual"]] == [None, "up", "down", "flat"]


def test_symbol_selection_default_and_unknown_fallback(tmp_path):
    db = build_db(tmp_path)
    d = view(tmp_path, db)["data"]
    assert d["symbol"] in ("US.NVDA", "US.TSLA") and d["codes"] == ["US.NVDA", "US.PDD", "US.TSLA"]    # 默认＝样本最多的标的
    d2 = view(tmp_path, db, symbol="US.PDD")["data"]
    assert d2["symbol"] == "US.PDD" and d2["chart"]["currency"] == "USD"
    d3 = view(tmp_path, db, symbol="US.NOPE")["data"]
    assert d3["symbol"] in ("US.NVDA", "US.TSLA") and any("US.NOPE" in w for w in d3["warnings"])


# ====================================================================== 最新预测与密封
def pending_pred(db, code="US.NVDA", **kw):
    kw.setdefault("tag", "forward")           # 密封只针对前向预测；rebuilt 不密封（见下方专门的测试）
    fw = dbmod.connect_writer(db, "forecast")
    insert_pred(fw, 8000 + len(code), code, SESS[-1], NEXT, "naive-vol-v1", SENT["y_low"], SENT["y_high"], prices=(SENT["low_price"], SENT["high_price"]), **kw)
    insert_pred(fw, 8100 + len(code), code, SESS[-1], NEXT, "lgbm-cqr-v1", SENT["y_low"], SENT["y_high"], prices=(SENT["low_price"], SENT["high_price"]), **kw)
    fw.close()


def latest(body, code, kind="latest"):
    return next(r for r in body["data"]["latest"] if r["code"] == code and r["kind"] == kind)


def test_unsettled_target_is_sealed_and_leaks_nothing(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    pending_pred(db)
    c = make_app(tmp_path, db).test_client()
    text = c.get("/api/v/forecast").get_data(as_text=True)
    for val in SENT.values():
        assert val not in text, val
    body = c.get("/api/v/forecast").get_json()
    r = latest(body, "US.NVDA")
    assert r["sealed"] and r["status"] == "已密封" and r["target"] == NEXT.isoformat() and r["as_of"] == SESS[-1].isoformat()
    assert r["models"] == {"baseline": None, "lgbm": None} and r["actual"] is None
    s = latest(body, "US.NVDA", "last_settled")                                                       # 另列最近一次目标日已结束的预测
    assert not s["sealed"] and s["status"] == "已结算" and s["as_of"] == SESS[39].isoformat()
    assert s["models"]["baseline"]["low"]["text"].startswith("99.00") or s["models"]["baseline"]["low"]["text"].startswith("97.00")
    assert s["actual"]["low"]["text"] == "98.00 USD" and s["actual"]["high"]["text"] == "103.00 USD"
    assert set(body["data"]["latest"][0]) >= {"code", "kind", "as_of", "target", "sealed"}


def test_sealed_state_holds_for_every_parameter_combination(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA", "US.TSLA"))
    pending_pred(db)
    c = make_app(tmp_path, db).test_client()
    for q in ("", "?source=rebuilt", "?source=forward", "?window=60", "?window=250&symbol=US.NVDA", "?symbol=US.TSLA&window=60"):
        text = c.get("/api/v/forecast" + q).get_data(as_text=True)
        for val in SENT.values():
            assert val not in text, (q, val)


def test_revealed_target_shows_levels_with_relative_position(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    pending_pred(db)
    add_reveal(db, target=NEXT.isoformat(), at="2026-03-11T05:45:00.000000Z")                          # 该市场该目标日已揭示
    r = latest(view(tmp_path, db), "US.NVDA")
    assert not r["sealed"] and r["status"] == "已揭示" and "last_settled" not in {x["kind"] for x in view(tmp_path, db)["data"]["latest"]}
    low = r["models"]["baseline"]["low"]
    assert low["text"] == "77.12 USD" and v(low) == D("77.1234")                                       # 价位带币种
    assert low["rel"]["text"] == "-22.88%" and low["rel"]["dir"] == "down"                              # 相对最新收盘价 100：红涨绿跌（跌＝down）
    assert r["models"]["lgbm"]["high"]["rel"]["dir"] == "up" and r["models"]["lgbm"]["high"]["rel"]["text"] == "+23.43%"
    assert r["actual"] is None                                                                          # 目标日尚无实际值


def test_reveal_for_another_market_does_not_unseal(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    pending_pred(db)
    w = dbmod.connect_writer(db, "coach")
    from mystock2.coach.intents import reveal
    reveal(w, batch_id="B1", market="HK", target_session=NEXT, channel="coach_show", version_hashes=["h"], at="2026-03-11T05:45:00.000000Z")
    reveal(w, batch_id="B1", market="US", target_session=NEXT + timedelta(days=7), channel="coach_show", version_hashes=["h"], at="2026-03-11T05:45:00.000000Z")
    w.close()
    text = make_app(tmp_path, db).test_client().get("/api/v/forecast").get_data(as_text=True)
    assert all(val not in text for val in SENT.values())


def test_future_dated_reveal_does_not_unseal(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    pending_pred(db)
    add_reveal(db, target=NEXT.isoformat(), at="2026-03-12T05:45:00.000000Z")                          # 晚于当前时间：保守视为未揭示
    assert latest(view(tmp_path, db), "US.NVDA")["sealed"]


def test_settled_latest_prediction_shows_levels_and_actual(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))                                                         # 最新 as_of=SESS[39]，目标日 SESS[40] 已有行情
    body = view(tmp_path, db)
    r = latest(body, "US.NVDA")
    assert not r["sealed"] and r["status"] == "已结算" and r["target"] == SESS[40].isoformat() and r["base_date"] == SESS[40].isoformat()
    assert r["models"]["baseline"]["low"]["rel"]["text"] and r["actual"]["high"]["text"] == "103.00 USD"
    assert not any(x["kind"] == "last_settled" for x in body["data"]["latest"])                         # 最新即已结算：不重复列


def test_latest_row_marks_missing_model_and_hk_currency(tmp_path):
    db = tmp_path / "m.db"
    dbmod.migrate(db)
    global SESS
    hk = sessions("HK", date(2026, 1, 5), 41)
    old, SESS = SESS, hk
    try:
        put_flat_quotes(db, "HK.00700", hk)
        fill_preds(db, "HK.00700", base=BASE_A, lgbm=None)
    finally:
        SESS = old
    r = latest(view(tmp_path, db), "HK.00700")
    assert r["currency"] == "HKD" and r["models"]["baseline"]["low"]["text"].endswith("HKD")
    lg = r["models"]["lgbm"]["low"]
    assert lg["na"] and lg["text"] == "不可用" and lg["v"] is None and "没有此 as_of" in lg["title"]


# ====================================================================== 新鲜度
def test_header_has_two_sources_and_notes_lagging_prediction(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    mk = dbmod.connect_writer(db, "market")
    put_daily(mk, [DailyBar("US.NVDA", date(2026, 3, 9), "100", "103", "98", "100", "100", "1")], source="synthetic", received_at=RECEIVED)   # 行情比最新预测的 as_of 更新
    mk.close()
    body = view(tmp_path, db)
    names = [s["name"] for s in body["header"]["sources"]]
    assert any("预测版本" in n for n in names) and "日线行情" in names
    assert body["header"]["staleness"]["label"] in ("新鲜", "陈旧", "未知") and body["header"]["data_mode"] == "daily"
    assert body["data"]["freshness"]["latest_quote_date"] == "2026-03-09"
    assert body["data"]["freshness"]["latest_prediction"]["as_of"] == SESS[39].isoformat()
    assert any("预测落后于行情" in n and "US.NVDA" in n for n in body["header"]["notes"])
    assert any("不是前向" in n or "rebuilt" in n for n in body["header"]["notes"])


def test_stale_data_is_not_called_fresh(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    c = make_app(tmp_path, db, clock=lambda: NOW + timedelta(days=30)).test_client()
    body = c.get("/api/v/forecast").get_json()
    assert body["header"]["staleness"]["label"] == "陈旧"


# ====================================================================== 框架/静态
def test_view_is_registered_with_params_and_default_order(tmp_path):
    js = make_app(tmp_path, build_db(tmp_path)).test_client().get("/api/views").get_json()
    assert not js["problems"]
    f = next(x for x in js["views"] if x["id"] == "forecast")
    names = {p["name"]: p for p in f["params"]}
    assert names["window"]["choices"] == [60, 120, 250] and names["window"]["default"] == 120
    assert names["source"]["choices"] == ["rebuilt", "forward"] and names["source"]["default"] == "rebuilt"
    ids = [x["id"] for x in js["views"]]
    assert ids.index("scoreboard") < ids.index("forecast") < ids.index("replay")


def test_query_lint_and_no_forbidden_dependencies():
    q = registry.BUILTIN_VIEWS_DIR / "forecast" / "query.py"
    assert registry.lint_query_source(q) == []
    src = q.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+mystock2\.(forecast|collectors|assistant)", src, re.M)
    assert "float(" not in src                                                   # 统计量全程 Decimal，时间经 now_of


def test_panel_wording_and_no_client_side_math():
    js = (registry.BUILTIN_VIEWS_DIR / "forecast" / "panel.js").read_text(encoding="utf-8")
    assert 'MS.registerPanel("forecast"' in js
    for bad in ("innerHTML", "Number(", "parseFloat", "toFixed", "打败", "win_rate"):
        assert bad not in js, bad
    assert "已密封" in js and "不足" in js and "不可用" in js


def test_predictions_outside_the_current_universe_are_not_shown(tmp_path):
    db = build_db(tmp_path)
    uni = tmp_path / "universe.yaml"
    uni.write_text("instruments:\n  - {code: US.NVDA, tier: trade}\n  - {code: US.TSLA, tier: trade}\n", encoding="utf-8")       # PDD 不在名单
    c = make_app(tmp_path, db, universe_path=str(uni)).test_client()
    code, body = get_view(c, "forecast")
    assert code == 200
    codes = {r["code"] for r in body["data"]["compare"]["rows"]}
    assert codes == {"US.NVDA", "US.TSLA"}                                              # 留档里仍有 PDD，但不再展示


def test_rebuilt_predictions_are_not_sealed_but_forward_ones_are(tmp_path):
    db = build_db(tmp_path, codes=("US.NVDA",))
    pending_pred(db, tag="rebuilt")
    row = latest(view(tmp_path, db), "US.NVDA")
    assert not row["sealed"] and row["status"] == "事后重建（未密封）"            # 负责人决定：事后重建的预测不属于前向样本，可看
    db2 = build_db(tmp_path / "b", codes=("US.NVDA",)) if (tmp_path / "b").mkdir() is None else None
    pending_pred(db2)                                                           # 前向（默认）
    assert latest(view(tmp_path / "b", db2), "US.NVDA")["sealed"]
