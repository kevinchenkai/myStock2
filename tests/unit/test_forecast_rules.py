import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from mystock2.core import calendars as cal
from mystock2.forecast.baseline import Bar, BaselineParams, ForecastUnavailable, bars_from_rows, predict
from mystock2.forecast.run import generate
from mystock2.forecast.versions import VersionError, latest_for_target, record_prediction
from mystock2.instruments.security_rule import RuleUnknown, parse_bands, put_rule, rule_for
from mystock2.market.bars import put_daily
from tests.unit.market_helpers import synth_bars, writers

UTC = timezone.utc
CODE = "US.NVDA"
P = BaselineParams(window=20, train_days=250, alpha_low=0.10, alpha_high=0.90, min_train=60)


def to_bars(dbars):
    return [Bar(b.session_date, Decimal(b.open), Decimal(b.high), Decimal(b.low), Decimal(b.close), Decimal(b.adj_close) if b.adj_close else None) for b in dbars]


# ------------------------------------------------------------ 基线预测器
def test_matches_independent_numpy_computation():
    dbars = synth_bars(CODE, 320, date(2026, 3, 4), seed=7)
    bars = to_bars(dbars)
    pred = predict(bars, P)
    adj = np.array([float(b.adj_close) for b in bars])
    ret = np.r_[np.nan, adj[1:] / adj[:-1] - 1]
    T = len(bars) - 1
    s = lambda i: np.std(ret[i - 19: i + 1], ddof=1)       # noqa: E731
    z_lo, z_hi = [], []
    for t in range(max(20, T - 250), T):
        f = float(bars[t + 1].adj_close) / float(bars[t + 1].close)
        z_lo.append((float(bars[t + 1].low) * f / adj[t] - 1) / s(t))
        z_hi.append((float(bars[t + 1].high) * f / adj[t] - 1) / s(t))
    exp_lo = np.quantile(z_lo, 0.10) * s(T)
    exp_hi = np.quantile(z_hi, 0.90) * s(T)
    assert float(pred.y_low) == pytest.approx(exp_lo, abs=1e-7)
    assert float(pred.y_high) == pytest.approx(exp_hi, abs=1e-7)
    assert float(pred.scale) == pytest.approx(s(T), abs=1e-7)
    assert pred.n_train == len(z_lo) and pred.as_of_session == bars[-1].session_date
    assert pred.low_price == bars[-1].close * (1 + pred.y_low)            # 价位用原始收盘价换算


def test_deterministic_and_uses_only_data_up_to_T():
    bars = to_bars(synth_bars(CODE, 330, date(2026, 3, 4), seed=3))
    a = predict(bars[:-1], P)                                            # T = 倒数第二根
    b = predict(bars[:-1], P)
    assert a == b
    # 把「T 之后」的一根 bar 改得面目全非，T 处的预测不变
    mutated = bars[:-1] + [Bar(bars[-1].session_date, Decimal(1), Decimal(1000), Decimal(1), Decimal(500), Decimal(500))]
    assert predict(mutated[:-1], P) == a
    # 预测区间含义：低价分位低于高价分位，且尺度为正
    assert a.y_low < a.y_high and a.scale > 0


def test_split_does_not_break_adjusted_returns():
    raw = synth_bars(CODE, 320, date(2026, 3, 4), seed=11)
    split_i = 200
    out = []
    for i, b in enumerate(raw):                                          # 构造 2:1 拆股：拆股后的原始价减半，复权价连续
        f = 0.5 if i >= split_i else 1.0
        out.append(Bar(b.session_date, Decimal(b.open) * Decimal(str(f)), Decimal(b.high) * Decimal(str(f)), Decimal(b.low) * Decimal(str(f)),
                       Decimal(b.close) * Decimal(str(f)), Decimal(b.adj_close)))
    # 拆股前原始价是拆股后的 2 倍：复权价序列不变
    clean = predict(to_bars(raw), P)
    split = predict(out, P)
    assert float(split.y_low) == pytest.approx(float(clean.y_low), abs=1e-4)   # 标签用复权口径，仅分位微差
    assert split.low_price < clean.low_price                                   # 但价位按（拆股后）原始收盘价换算


def test_unavailable_not_extrapolated():
    bars = to_bars(synth_bars(CODE, 330, date(2026, 3, 4)))
    with pytest.raises(ForecastUnavailable, match="历史不足"):
        predict(bars[:10], P)
    with pytest.raises(ForecastUnavailable, match="训练样本不足"):
        predict(bars[:70], P)
    no_adj = [Bar(b.session_date, b.open, b.high, b.low, b.close, None) for b in bars]
    with pytest.raises(ForecastUnavailable, match="复权"):
        predict(no_adj, P)
    flat = [Bar(b.session_date, Decimal(10), Decimal(10), Decimal(10), Decimal(10), Decimal(10)) for b in bars]
    with pytest.raises(ForecastUnavailable, match="尺度"):
        predict(flat, P)
    with pytest.raises(ForecastUnavailable):
        predict(bars, BaselineParams(alpha_low=0.9, alpha_high=0.1))
    unordered = list(reversed(bars))
    with pytest.raises(ForecastUnavailable, match="递增"):
        predict(unordered, P)


def test_in_sample_coverage_is_roughly_the_chosen_quantile():
    """合成随机游走上：次日低价低于预测低价的频率应接近 α_low（宽松校准检验，不是收益证明）。"""
    dbars = synth_bars(CODE, 900, date(2026, 3, 4), seed=21, vol=0.02)
    bars = to_bars(dbars)
    hits = total = 0
    for T in range(300, len(bars) - 1, 5):
        pr = predict(bars[: T + 1], P)
        nxt = bars[T + 1]
        f = float(nxt.adj_close) / float(nxt.close)
        total += 1
        hits += (float(nxt.low) * f / float(bars[T].adj_close) - 1) < float(pr.y_low)
    assert 0.03 < hits / total < 0.20


# ------------------------------------------------------------ 预测版本与点时生成
def test_generate_records_immutable_versions_with_evidence(tmp_path):
    m, f, i, path = writers(tmp_path)
    dbars = synth_bars(CODE, 330, date(2026, 3, 4), seed=5)
    recv = datetime(2026, 3, 4, 22, 0, tzinfo=UTC)
    put_daily(m, dbars, source="s", received_at=recv, quality="ok")
    pid = generate(m, f, CODE, date(2026, 3, 4), input_cutoff_at=recv + timedelta(minutes=1), now=recv + timedelta(minutes=2), params=P)
    row = latest_for_target(f, CODE, date(2026, 3, 5))
    assert row["prediction_id"] == pid and row["as_of_session"] == "2026-03-04" and row["source_tag"] == "forward"
    assert Decimal(row["y_low"]) < Decimal(row["y_high"]) and row["input_snapshot_ids"] != "[]"
    # 重复生成：同一输入同一版本，幂等
    assert generate(m, f, CODE, date(2026, 3, 4), input_cutoff_at=recv + timedelta(minutes=1), now=recv + timedelta(minutes=3), params=P) == pid
    with pytest.raises(sqlite3.DatabaseError, match="不可覆盖"):
        f.execute("UPDATE prediction_version SET y_low='0'")
    with pytest.raises(sqlite3.DatabaseError):                                        # forecast 写入者不能写账本/行情
        f.execute("INSERT INTO quote_daily(code) VALUES ('x')")


def test_t38_late_arriving_data_cannot_enter_an_earlier_cutoff(tmp_path):
    m, f, i, path = writers(tmp_path)
    dbars = synth_bars(CODE, 330, date(2026, 3, 4), seed=5)
    early, late = datetime(2026, 3, 4, 22, 0, tzinfo=UTC), datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
    put_daily(m, dbars[:-1], source="s", received_at=early, quality="ok")
    put_daily(m, dbars[-1:], source="s", received_at=late, quality="ok")        # T 的行情很晚才收到
    with pytest.raises(ForecastUnavailable, match="终值日线"):                    # 截止在 early：不得用旧数据冒充
        generate(m, f, CODE, date(2026, 3, 4), input_cutoff_at=early + timedelta(minutes=1), params=P)
    pid = generate(m, f, CODE, date(2026, 3, 4), input_cutoff_at=late + timedelta(minutes=1), params=P, now=late + timedelta(minutes=2))
    assert pid


def test_partial_bar_not_used_and_calendar_checked(tmp_path):
    m, f, i, path = writers(tmp_path)
    dbars = synth_bars(CODE, 330, date(2026, 3, 4), seed=5)
    put_daily(m, dbars[:-1], source="s", received_at=datetime(2026, 3, 4, 22, tzinfo=UTC), quality="ok")
    put_daily(m, dbars[-1:], source="s", received_at=datetime(2026, 3, 4, 20, tzinfo=UTC), quality="partial")   # 未走完
    with pytest.raises(ForecastUnavailable):
        generate(m, f, CODE, date(2026, 3, 4), input_cutoff_at=datetime(2026, 3, 5, tzinfo=UTC), params=P)
    with pytest.raises(ForecastUnavailable, match="不是"):
        generate(m, f, CODE, date(2026, 3, 7), input_cutoff_at=datetime(2026, 3, 8, tzinfo=UTC), params=P)


def test_record_prediction_rejects_bad_time_chain_and_missing_evidence(tmp_path):
    m, f, i, path = writers(tmp_path)
    bars = to_bars(synth_bars(CODE, 330, date(2026, 3, 4), seed=5))
    pred = predict(bars, P)
    t = datetime(2026, 3, 4, 22, tzinfo=UTC)
    with pytest.raises(VersionError, match="forward_requires_evidence"):
        record_prediction(f, CODE, pred, P, [], input_cutoff_at=t, generated_at=t, available_at=t, source_tag="forward")
    with pytest.raises(VersionError, match="missing_snapshot"):
        record_prediction(f, CODE, pred, P, ["nope"], input_cutoff_at=t, generated_at=t, available_at=t, source_tag="rebuilt")
    with pytest.raises(VersionError, match="source_tag"):
        record_prediction(f, CODE, pred, P, [], input_cutoff_at=t, generated_at=t, available_at=t, source_tag="live")
    # 无证据的「重建」允许写入但带 rebuilt 标签（事后重建不冒充前向）
    pid = record_prediction(f, CODE, pred, P, [], input_cutoff_at=t, generated_at=t, available_at=t, source_tag="rebuilt")
    assert latest_for_target(f, CODE, date(2026, 3, 5), "rebuilt")["prediction_id"] == pid
    assert latest_for_target(f, CODE, date(2026, 3, 5), "forward") is None
    bad = t - timedelta(minutes=1)
    with pytest.raises(VersionError, match="chain"):
        record_prediction(f, CODE, pred, P, [], input_cutoff_at=t, generated_at=bad, available_at=bad, source_tag="rebuilt")


def test_bars_from_rows_roundtrip(tmp_path):
    m, f, i, path = writers(tmp_path)
    put_daily(m, synth_bars(CODE, 5, date(2026, 3, 4)), source="s", quality="ok")
    rows = m.execute("SELECT * FROM quote_daily ORDER BY session_date").fetchall()
    assert [b.session_date for b in bars_from_rows(rows)] == [date.fromisoformat(r["session_date"]) for r in rows]
    assert cal.is_session("US", bars_from_rows(rows)[0].session_date)


# ------------------------------------------------------------ 证券规则
BANDS = '[{"lt":"0.25","tick":"0.001"},{"lt":"0.5","tick":"0.005"},{"tick":"0.01"}]'


def test_security_rule_unknown_unverified_and_bands(tmp_path):
    m, f, inst, path = writers(tmp_path)
    with pytest.raises(RuleUnknown, match="没有证券规则"):
        rule_for(inst, "HK.00700", "2026-03-04")
    put_rule(inst, "HK.00700", "2026-01-01", lot_size=100, tick_json=BANDS, source="合成测试", verified=False)
    with pytest.raises(RuleUnknown, match="尚未核实"):
        rule_for(inst, "HK.00700", "2026-03-04")
    r = rule_for(inst, "HK.00700", "2026-03-04", require_verified=False)     # 开发期可显式放宽，正式出单必须核实
    assert r.lot_size == 100 and r.tick_for("0.2") == Decimal("0.001") and r.tick_for("0.3") == Decimal("0.005") and r.tick_for("9") == Decimal("0.01")
    with pytest.raises(RuleUnknown):
        rule_for(inst, "HK.00700", "2025-12-31", require_verified=False)     # 生效期之前
    put_rule(inst, "HK.00700", "2026-06-01", lot_size=200, tick_json=BANDS, source="合成", verified=True)
    assert rule_for(inst, "HK.00700", "2026-07-01").lot_size == 200           # 版本随生效期切换
    assert rule_for(inst, "HK.00700", "2026-03-04", require_verified=False).lot_size == 100


def test_legal_limit_rounds_conservatively_across_bands(tmp_path):
    m, f, inst, path = writers(tmp_path)
    put_rule(inst, "HK.00700", "2026-01-01", lot_size=100, tick_json=BANDS, source="合成", verified=True)
    r = rule_for(inst, "HK.00700", "2026-03-04")
    assert r.legal_limit("0.2437", "BUY") == Decimal("0.243")      # 买价向下
    assert r.legal_limit("0.2433", "SELL") == Decimal("0.244")     # 卖价向上
    assert r.legal_limit("0.2495", "SELL") == Decimal("0.250")     # 向上到档位边界；0.25 已属于下一档（tick 0.005），0.250 合法
    assert r.legal_limit("0.253", "BUY") == Decimal("0.250")       # 新档位 tick=0.005，向下舍入到 0.250
    assert r.legal_limit("12.347", "BUY") == Decimal("12.34")


def test_bad_tick_config_rejected(tmp_path):
    m, f, inst, path = writers(tmp_path)
    with pytest.raises(ValueError):
        parse_bands('[{"lt":"1","tick":"0.01"},{"lt":"0.5","tick":"0.001"}]')   # 非升序
    with pytest.raises(ValueError):
        parse_bands('[{"tick":"0.01"},{"lt":"1","tick":"0.1"}]')                # 兜底项必须在最后
    with pytest.raises(ValueError):
        put_rule(inst, "HK.00700", "2026-01-01", lot_size=100, tick_json='[{"lt":"1","tick":"0.01"},{"lt":"0.5","tick":"0.001"}]', source="x")
