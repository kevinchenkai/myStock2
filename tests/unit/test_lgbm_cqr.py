from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from mystock2.forecast import baseline, lgbm_cqr
from mystock2.forecast.baseline import Bar, ForecastUnavailable
from mystock2.forecast.evaluate import EvalResult, improvement, meets_gate, pinball, rolling_eval
from mystock2.forecast.lgbm_cqr import (
    FEATURE_COLS,
    LGBMParams,
    build_features,
    build_labels,
    conformal_level,
    conformal_score,
)
from tests.unit.market_helpers import synth_bars

CODE = "US.NVDA"
FAST = LGBMParams(n_rounds=40, min_train=200, train_days=500)


def to_bars(dbars):
    return [Bar(b.session_date, Decimal(b.open), Decimal(b.high), Decimal(b.low), Decimal(b.close), Decimal(b.adj_close) if b.adj_close else None,
                Decimal(b.volume) if b.volume else None) for b in dbars]


def test_pinball_hand_values_and_conformal_level():
    assert pinball(1.0, 0.0, 0.9) == pytest.approx(0.9)          # y>q：α(y−q)
    assert pinball(-1.0, 0.0, 0.9) == pytest.approx(0.1)         # y<q：(1−α)(q−y)
    assert pinball(0.0, 0.0, 0.1) == 0
    assert conformal_level(99, 0.9) == pytest.approx(90 / 99) and conformal_level(10, 0.99) == 1.0 and conformal_level(100, 0.9) == pytest.approx(91 / 100)


def test_features_use_only_data_up_to_t_and_labels_use_next_day():
    bars = to_bars(synth_bars(CODE, 120, date(2026, 3, 4), seed=1))
    X, _ = build_features(bars)
    mutated = bars[:80] + [Bar(b.session_date, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.adj_close * 3, b.volume) for b in bars[80:]]
    X2, _ = build_features(mutated)
    assert np.allclose(X[:80], X2[:80], equal_nan=True)            # t<80 的特征不受 t≥80 的改动影响
    yl, yh = build_labels(bars)
    f = float(bars[11].adj_close) / float(bars[11].close)
    assert yl[10] == pytest.approx(float(bars[11].low) * f / float(bars[10].adj_close) - 1) and np.isnan(yl[-1])
    assert np.isfinite(X[40]).all() and list(range(len(FEATURE_COLS))) == list(range(16))


def test_predict_is_deterministic_ordered_calibrated_and_unavailable_cases():
    bars = to_bars(synth_bars(CODE, 600, date(2026, 3, 4), seed=3))
    a, b = lgbm_cqr.predict(bars, FAST), lgbm_cqr.predict(bars, FAST)
    assert a == b                                                           # 确定性
    assert a.y_low < a.y_high and a.low_price == bars[-1].close * (1 + a.y_low) and a.n_train > 100
    with pytest.raises(ForecastUnavailable, match="历史不足"):
        lgbm_cqr.predict(bars[:20], FAST)
    with pytest.raises(ForecastUnavailable, match="训练样本不足"):
        lgbm_cqr.predict(bars[:150], FAST)
    with pytest.raises(ForecastUnavailable, match="复权"):
        lgbm_cqr.predict([Bar(x.session_date, x.open, x.high, x.low, x.close, None, x.volume) for x in bars], FAST)
    with pytest.raises(ForecastUnavailable, match="分位"):
        lgbm_cqr.predict(bars, LGBMParams(alpha_low=0.9, alpha_high=0.1))
    no_vol = [Bar(x.session_date, x.open, x.high, x.low, x.close, x.adj_close, None) for x in bars]
    with pytest.raises(ForecastUnavailable):                                # 成交量缺失 → 特征含缺失：不外推
        lgbm_cqr.predict(no_vol, FAST)


def test_prediction_at_T_ignores_future_bars_beyond_T():
    bars = to_bars(synth_bars(CODE, 620, date(2026, 3, 4), seed=5))
    ref = lgbm_cqr.predict(bars[:600], FAST)
    poisoned = bars[:600] + [Bar(x.session_date, x.open * 9, x.high * 9, x.low * 9, x.close * 9, x.adj_close * 9, x.volume) for x in bars[600:]]
    assert lgbm_cqr.predict(poisoned[:600], FAST) == ref


def test_missing_lightgbm_fails_loudly_not_silently_falling_back(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "lightgbm":
            raise ImportError("no lightgbm")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ForecastUnavailable, match="不静默回退"):
        lgbm_cqr.predict(to_bars(synth_bars(CODE, 600, date(2026, 3, 4), seed=3)), FAST)


def test_rolling_eval_mechanics_and_calibrated_coverage_is_near_target():
    bars = to_bars(synth_bars(CODE, 700, date(2026, 3, 4), seed=21, vol=0.02))
    res = rolling_eval(bars, lambda h: lgbm_cqr.predict(h, FAST), start=520, step=6)
    base = rolling_eval(bars, lambda h: baseline.predict(h), start=520, step=6)
    assert res.n == base.n and res.n > 20 and res.skipped == 0
    assert 0.0 <= res.cover_low <= 0.35 and 0.0 <= res.cover_high <= 0.35                    # 宽松：目标 10%
    assert res.total > 0 and base.total > 0
    again = rolling_eval(bars, lambda h: lgbm_cqr.predict(h, FAST), start=520, step=6)
    assert again == res                                                                       # 评估确定性
    with pytest.raises(ForecastUnavailable):
        rolling_eval(bars[:30], lambda h: baseline.predict(h), start=10)


def test_gate_logic_and_improvement_sign():
    a = EvalResult(100, 0.010, 0.010, 0.1, 0.1, 0)
    b = EvalResult(100, 0.009, 0.0095, 0.1, 0.1, 0)
    assert improvement(a, b) == pytest.approx(0.075) and improvement(b, a) < 0
    low = meets_gate({"A": 0.08, "B": 0.06, "C": -0.01})
    assert not low["pass"] and low["names_improved"] == 2 and low["names_needed"] == 2 and low["mean_improvement"] == pytest.approx(0.0433, abs=1e-3)   # 平均 <5%
    assert not meets_gate({"A": 0.2, "B": -0.05, "C": -0.05})["pass"]                         # 平均够但改善的标的不够
    assert not meets_gate({"A": 0.04, "B": 0.04, "C": 0.04})["pass"]                          # 每个都改善但不到 5%
    assert meets_gate({"A": 0.06, "B": 0.07, "C": 0.05})["pass"]


def test_f02_conformal_score_takes_the_kth_smallest_not_one_more():
    """审核 F-02：n=100、level=0.9 → 取第 ⌈101×0.9⌉＝91 小（v1 用 np.quantile(…, 0.91, 'higher') 取到第 92 小）。"""
    s = np.arange(100, dtype=float)[::-1]          # 第 k 小＝k−1
    assert conformal_score(s, 0.9) == 90.0
    assert conformal_score(np.arange(10, dtype=float), 0.99) == 9.0     # k 超过 n：取最大
    assert conformal_score(np.arange(99, dtype=float), 0.9) == 89.0     # ⌈100×0.9⌉＝90 → 第 90 小


def test_zero_volume_days_are_skipped_in_volume_ratio_windows_not_poisoning_later_rows():
    """FC-1：成交量为 0 的日子在量比窗口内跳过；只有该日自己的量比缺失，其后的日子照常有值；没有 0 时与完整窗口均值一致。"""
    bars = to_bars(synth_bars(CODE, 120, date(2026, 3, 4), seed=1))
    X, _ = build_features(bars)
    z = 60
    zb = [Bar(b.session_date, b.open, b.high, b.low, b.close, b.adj_close, Decimal(0)) if i == z else b for i, b in enumerate(bars)]
    Xz, _ = build_features(zb)
    j5, j20 = FEATURE_COLS.index("vol_ratio_5"), FEATURE_COLS.index("vol_ratio_20")
    assert np.isnan(Xz[z, j5]) and np.isnan(Xz[z, j20])                              # 0 成交量当天：量比缺失
    assert np.isfinite(Xz[z + 1:, j5]).all() and np.isfinite(Xz[z + 1:, j20]).all()  # 其后窗口跳过该日，仍有值（以前整整 20 天都缺失）
    assert np.allclose(X[:z], Xz[:z], equal_nan=True)                                # 之前不受影响
    other = [i for i in range(len(FEATURE_COLS)) if i not in (j5, j20)]
    assert np.allclose(X[:, other], Xz[:, other], equal_nan=True)                    # 非成交量特征不变
    w = [float(b.volume) for b in bars[z - 3:z + 2] if b is not bars[z]]            # z+2 的 5 日窗口含 z：跳过后取其余 4 日均值
    ref = float(bars[z + 2].volume) / np.mean([float(bars[i].volume) for i in range(z - 2, z + 3) if i != z])
    assert Xz[z + 2, j5] == pytest.approx(ref) and len(w) == 4
    assert lgbm_cqr.MODEL_VERSION == "lgbm-cqr-v3"
