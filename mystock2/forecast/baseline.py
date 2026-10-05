"""透明基线预测器（实施方案 WP4.5；V1 `naive_vol` 思路的重写，纯 Python，无 numpy 依赖）。

目标定义（与全项目一致）：`y_low = low_{T+1}/close_T − 1`，`y_high = high_{T+1}/close_T − 1`。

方法（标准化经验分位）：
  1. 对每个训练日 t（t ≤ T−1，标签 t→t+1 已成熟）算尺度 s_t＝过去 W 个日收益的样本标准差（只用 ≤ t 的数据）；
  2. 标准化标签 z_low = y_low,t / s_t，z_high = y_high,t / s_t；
  3. 取训练窗口内 z 的经验分位 q_low = quantile(z_low, α_low)、q_high = quantile(z_high, α_high)；
  4. T+1 预测：y_low = q_low × s_T，y_high = q_high × s_T；价位 = close_T × (1 + y)。
用复权价算收益与标签比例（避免拆股/除息跳变），用**原始收盘价**换算价位（价位要对应真实成交价）。

确定性：同样输入必得同样输出；严格不使用 T 之后的数据（有测试：扰动未来 bar 不改变结果）。
参数（窗口、分位）属于冻结的版本配置，**不依据事后高低点调整**；`model_version` 随参数变化。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mystock2.core.money import dec, to_db

MODEL_VERSION = "naive-vol-v1"
FEATURE_VERSION = "f-ret20-adjohlc-v1"


class ForecastUnavailable(ValueError):
    """输入不足或不合格：输出「不可用」，不外推、不补默认值。"""


@dataclass(frozen=True)
class BaselineParams:
    window: int = 20            # 尺度窗口（日收益个数）
    train_days: int = 250       # 训练窗口（标注行数）
    alpha_low: float = 0.10     # 低价分位（版本参数，冻结后不改）
    alpha_high: float = 0.90
    min_train: int = 60         # 低于此样本数则不可用

    def as_dict(self) -> dict:
        return {"window": self.window, "train_days": self.train_days, "alpha_low": self.alpha_low,
                "alpha_high": self.alpha_high, "min_train": self.min_train}


@dataclass(frozen=True)
class Bar:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal            # 原始收盘价
    adj_close: Decimal | None
    volume: Decimal | None = None


@dataclass(frozen=True)
class Prediction:
    y_low: Decimal
    y_high: Decimal
    low_price: Decimal
    high_price: Decimal
    scale: Decimal
    n_train: int
    as_of_session: date


def bars_from_rows(rows) -> list[Bar]:
    return [Bar(date.fromisoformat(r["session_date"]), dec(r["open"]), dec(r["high"]), dec(r["low"]), dec(r["close"]),
                dec(r["adj_close"]) if r["adj_close"] is not None else None, dec(r["volume"]) if r["volume"] is not None else None) for r in rows]


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位（与 numpy 默认 'linear' 一致）。"""
    n = len(sorted_vals)
    pos = q * (n - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def predict(bars: list[Bar], params: BaselineParams | None = None) -> Prediction:
    """bars 按日期升序，最后一根即 T。每根必须有复权收盘价；日期必须严格递增。"""
    params = params or BaselineParams()
    if not (0 < params.alpha_low < params.alpha_high < 1):
        raise ForecastUnavailable("分位参数不合法")
    if len(bars) < params.window + 2:
        raise ForecastUnavailable(f"历史不足：需要至少 {params.window + 2} 根日线，现有 {len(bars)}")
    for a, b in zip(bars, bars[1:], strict=False):
        if not a.session_date < b.session_date:
            raise ForecastUnavailable("日线日期必须严格递增")
    if any(b.adj_close is None or b.adj_close <= 0 for b in bars):
        raise ForecastUnavailable("缺少复权收盘价，无法计算收益")
    adj = [float(b.adj_close) for b in bars]
    ret = [None] + [adj[i] / adj[i - 1] - 1.0 for i in range(1, len(bars))]
    w = params.window

    def scale_at(i: int) -> float | None:        # 只用 ≤ i 的收益
        if i < w:
            return None
        xs = ret[i - w + 1: i + 1]
        mean = sum(xs) / w
        var = sum((x - mean) ** 2 for x in xs) / (w - 1)
        return math.sqrt(var)

    T = len(bars) - 1
    s_T = scale_at(T)
    if s_T is None or s_T <= 0:
        raise ForecastUnavailable("尺度不可用（波动为 0 或窗口不足）")

    z_low, z_high = [], []
    first = max(w, T - params.train_days)
    for t in range(first, T):                    # 标签 t→t+1 需要 t+1 ≤ T，已成熟
        s = scale_at(t)
        if s is None or s <= 0:
            continue
        f = adj[t + 1] / float(bars[t + 1].close)      # 复权因子（用于把 t+1 的 OHLC 调到复权口径）
        base = adj[t]
        z_low.append((float(bars[t + 1].low) * f / base - 1.0) / s)
        z_high.append((float(bars[t + 1].high) * f / base - 1.0) / s)
    if len(z_low) < params.min_train:
        raise ForecastUnavailable(f"训练样本不足：{len(z_low)} < {params.min_train}")
    q_low = _quantile(sorted(z_low), params.alpha_low)
    q_high = _quantile(sorted(z_high), params.alpha_high)
    y_low, y_high = q_low * s_T, q_high * s_T
    if y_low > y_high:
        raise ForecastUnavailable("预测区间倒挂")
    close = bars[T].close
    yl, yh = Decimal(f"{y_low:.8f}"), Decimal(f"{y_high:.8f}")
    return Prediction(yl, yh, close * (1 + yl), close * (1 + yh), Decimal(f"{s_T:.8f}"), len(z_low), bars[T].session_date)


def prediction_fields(p: Prediction) -> dict:
    return {"y_low": to_db(p.y_low), "y_high": to_db(p.y_high), "low_price": to_db(p.low_price.quantize(Decimal("0.0001"))),
            "high_price": to_db(p.high_price.quantize(Decimal("0.0001"))), "scale": to_db(p.scale), "n_train": p.n_train}
