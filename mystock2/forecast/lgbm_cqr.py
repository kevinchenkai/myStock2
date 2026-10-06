"""LightGBM 分位回归 + 单侧 CQR 校准（M8 候选线 `ai_lgbm` 的预测器；实施方案 §5 M8、§4.1）。

与基线同一目标与口径：`y_low = low_{T+1}/close_T − 1`、`y_high = high_{T+1}/close_T − 1`（**复权**比例；价位用原始收盘价换算）。
借用 V1 的 16 个特征（只用 ≤ T 的信息，冻结版本 `f-v1-16`），代码重写，并修复 V1 的两处问题：
  * V1 标签用「原始次日价/原始今日价」，拆股日会错；V2 统一用复权口径；
  * V1 缺 LightGBM 时**静默回退 sklearn**；V2 缺依赖/拟合失败**明确失败**（`ForecastUnavailable`），不静默换模型。
单侧 CQR：把标签窗口按时间切成「训练 | 隔离 | 校准」，校准段上把低/高分位分别平移，使校准段的经验覆盖率达到目标
（有限样本修正的分位水平 ⌈(n+1)(1−α)⌉/n）。分位模型分别预测，**不保证**联合覆盖。
确定性：固定 seed、单线程、deterministic；同输入同输出。参数属冻结版本配置，改动＝新 `model_version`。
本预测器尚未证明优于基线：晋级沿用 V1 门槛并用 `forecast/evaluate.py` 评估；M8 以**新比较批次**加入，不影响已有线。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from mystock2.forecast.baseline import Bar, ForecastUnavailable, Prediction

MODEL_VERSION = "lgbm-cqr-v2"     # v2：校准分数取第 ⌈(n+1)·level⌉ 小（v1 经 np.quantile 多取一名，区间偏宽；审核 F-02/Q2）
FEATURE_VERSION = "f-v1-16"
FEATURE_COLS = ["ret_1d", "ret_5d", "ret_10d", "vol_5d", "vol_20d", "atr_14", "ma5_dev", "ma10_dev", "ma20_dev", "close_pos_in_range",
                "day_range_rel", "gap", "dist_hi_20", "dist_lo_20", "vol_ratio_5", "vol_ratio_20"]


@dataclass(frozen=True)
class LGBMParams:
    alpha_low: float = 0.10
    alpha_high: float = 0.90
    train_days: int = 750
    calib_frac: float = 0.2
    purge: int = 1
    min_train: int = 250
    n_rounds: int = 200
    learning_rate: float = 0.05
    num_leaves: int = 15
    min_data_in_leaf: int = 20
    feature_fraction: float = 0.8
    seed: int = 7

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _roll(a: np.ndarray, w: int, fn) -> np.ndarray:
    out = np.full(len(a), np.nan)
    for i in range(w - 1, len(a)):
        out[i] = fn(a[i - w + 1: i + 1])
    return out


def build_features(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray]:
    """返回 (特征矩阵[n,16]，成交量缺失时对应列为 NaN)。第 i 行只用 ≤ i 的 bar。bars 需含 adj_close；volume 用 bar 的原始成交量（Bar 无该字段时置 NaN）。"""
    n = len(bars)
    close = np.array([float(b.close) for b in bars])
    adj = np.array([float(b.adj_close) for b in bars])
    ratio = adj / close
    hi = np.array([float(b.high) for b in bars]) * ratio
    lo = np.array([float(b.low) for b in bars]) * ratio
    op = np.array([float(b.open) for b in bars]) * ratio
    vol = np.array([float(getattr(b, "volume", np.nan) or np.nan) for b in bars])
    ret1 = np.r_[np.nan, adj[1:] / adj[:-1] - 1]
    X = np.full((n, len(FEATURE_COLS)), np.nan)

    def pct(k):
        out = np.full(n, np.nan)
        out[k:] = adj[k:] / adj[:-k] - 1
        return out

    def std(a, w):
        return _roll(a, w, lambda x: np.std(x, ddof=1) if np.all(np.isfinite(x)) else np.nan)

    def mean(a, w):
        return _roll(a, w, lambda x: np.mean(x) if np.all(np.isfinite(x)) else np.nan)

    prev = np.r_[np.nan, adj[:-1]]
    tr = np.nanmax(np.vstack([hi - lo, np.abs(hi - prev), np.abs(lo - prev)]), axis=0)
    rng = np.where(hi - lo == 0, np.nan, hi - lo)
    cols = {
        "ret_1d": ret1, "ret_5d": pct(5), "ret_10d": pct(10), "vol_5d": std(ret1, 5), "vol_20d": std(ret1, 20),
        "atr_14": mean(tr, 14) / adj, "ma5_dev": adj / mean(adj, 5) - 1, "ma10_dev": adj / mean(adj, 10) - 1, "ma20_dev": adj / mean(adj, 20) - 1,
        "close_pos_in_range": (adj - lo) / rng, "day_range_rel": rng / adj, "gap": op / prev - 1,
        "dist_hi_20": adj / _roll(hi, 20, np.max) - 1, "dist_lo_20": adj / _roll(lo, 20, np.min) - 1,
        "vol_ratio_5": vol / mean(vol, 5), "vol_ratio_20": vol / mean(vol, 20),
    }
    for j, name in enumerate(FEATURE_COLS):
        X[:, j] = cols[name]
    return X, adj


def build_labels(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray]:
    """标签（复权口径）：row t 的 y_low/y_high 用 t+1 的 bar；最后一行为 NaN。"""
    n = len(bars)
    adj = np.array([float(b.adj_close) for b in bars])
    close = np.array([float(b.close) for b in bars])
    f = adj / close
    low = np.array([float(b.low) for b in bars]) * f
    high = np.array([float(b.high) for b in bars]) * f
    yl, yh = np.full(n, np.nan), np.full(n, np.nan)
    yl[:-1] = low[1:] / adj[:-1] - 1
    yh[:-1] = high[1:] / adj[:-1] - 1
    return yl, yh


def conformal_level(n: int, level: float) -> float:
    """有限样本修正的分位水平 ⌈(n+1)·level⌉/n，上限 1。"""
    return min(1.0, math.ceil((n + 1) * level) / n)


def conformal_score(scores: np.ndarray, level: float) -> float:
    """校准分数的第 k 小值，k＝⌈(n+1)·level⌉（超过 n 时取最大值，即上限 1 的情形）。"""
    n = len(scores)
    k = min(n, math.ceil((n + 1) * level))
    return float(np.sort(scores)[k - 1])


def _fit(X, y, alpha: float, p: LGBMParams):
    import lightgbm as lgb  # 缺依赖明确报 ImportError，不静默回退
    params = {"objective": "quantile", "alpha": alpha, "learning_rate": p.learning_rate, "num_leaves": p.num_leaves, "min_data_in_leaf": p.min_data_in_leaf,
              "feature_fraction": p.feature_fraction, "seed": p.seed, "num_threads": 1, "deterministic": True, "force_row_wise": True, "verbosity": -1,
              "feature_fraction_seed": p.seed, "bagging_seed": p.seed, "data_random_seed": p.seed}
    return lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=p.n_rounds)


def predict(bars: list[Bar], params: LGBMParams | None = None) -> Prediction:
    p = params or LGBMParams()
    if not (0 < p.alpha_low < p.alpha_high < 1):
        raise ForecastUnavailable("分位参数不合法")
    if len(bars) < 40:
        raise ForecastUnavailable(f"历史不足：{len(bars)} < 40")
    for a, b in zip(bars, bars[1:], strict=False):
        if not a.session_date < b.session_date:
            raise ForecastUnavailable("日线日期必须严格递增")
    if any(b.adj_close is None or b.adj_close <= 0 for b in bars):
        raise ForecastUnavailable("缺少复权收盘价，无法计算特征")
    X, adj = build_features(bars)
    yl, yh = build_labels(bars)
    T = len(bars) - 1
    first = max(0, T - p.train_days)
    idx = [t for t in range(first, T) if np.all(np.isfinite(X[t])) and np.isfinite(yl[t]) and np.isfinite(yh[t])]      # 标签 t→t+1 已成熟（t ≤ T−1）
    if len(idx) < p.min_train:
        raise ForecastUnavailable(f"训练样本不足：{len(idx)} < {p.min_train}")
    if not np.all(np.isfinite(X[T])):
        raise ForecastUnavailable("T 日特征含缺失（窗口不足或成交量缺失），不外推")
    n_cal = max(30, int(len(idx) * p.calib_frac))
    train_idx, cal_idx = idx[: len(idx) - n_cal - p.purge], idx[len(idx) - n_cal:]
    if len(train_idx) < p.min_train // 2:
        raise ForecastUnavailable("训练段过短")
    Xtr, Xcal = X[train_idx], X[cal_idx]
    try:
        m_lo, m_hi = _fit(Xtr, yl[train_idx], p.alpha_low, p), _fit(Xtr, yh[train_idx], p.alpha_high, p)
    except ImportError as exc:
        raise ForecastUnavailable(f"LightGBM 不可用（不静默回退其他模型）：{exc}") from exc
    # 单侧 CQR：把低/高分位分别平移到校准段覆盖率达标
    s_lo = m_lo.predict(Xcal) - yl[cal_idx]               # >0 表示 y 低于预测低分位
    s_hi = yh[cal_idx] - m_hi.predict(Xcal)               # >0 表示 y 高于预测高分位
    adj_lo = conformal_score(s_lo, 1 - p.alpha_low)
    adj_hi = conformal_score(s_hi, p.alpha_high)
    y_low = float(m_lo.predict(X[T:T + 1])[0]) - adj_lo
    y_high = float(m_hi.predict(X[T:T + 1])[0]) + adj_hi
    if not (np.isfinite(y_low) and np.isfinite(y_high)) or y_low > y_high:
        raise ForecastUnavailable("预测区间不合法（非有限或倒挂）")
    close = bars[T].close
    yl_d, yh_d = Decimal(f"{y_low:.8f}"), Decimal(f"{y_high:.8f}")
    scale = Decimal(f"{float(X[T, FEATURE_COLS.index('vol_20d')]):.8f}")
    return Prediction(yl_d, yh_d, close * (1 + yl_d), close * (1 + yh_d), scale, len(train_idx), bars[T].session_date)
