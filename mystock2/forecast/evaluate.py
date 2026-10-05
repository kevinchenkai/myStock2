"""预测评估（时间有序、滚动起点；实施方案 §4.1、§4.4）。

- 指标：分位损失（pinball）、覆盖率（实际低价低于预测低价的频率、实际高价高于预测高价的频率）。
- 评估只用「截至 T 的数据预测 T+1」，标签在评估时才揭示；**不随机打散日期**。
- 晋级门槛沿用 V1（预测层，与策略收益分别验收，不得事后调低）：等权 raw pinball 改善 ≥ 5%，且至少 `min_names_improved`/`n_names` 个标的改善。
- 本模块只回答「预测更准吗」，**不回答「更赚钱吗」**；策略收益由记分牌在前向批次里验收。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mystock2.forecast.baseline import Bar, ForecastUnavailable, Prediction


def pinball(y: float, q: float, alpha: float) -> float:
    d = y - q
    return max(alpha * d, (alpha - 1) * d)


@dataclass
class EvalResult:
    n: int
    pinball_low: float
    pinball_high: float
    cover_low: float          # P(y_low < 预测低价)，应接近 α_low
    cover_high: float         # P(y_high > 预测高价)，应接近 1−α_high
    skipped: int

    @property
    def total(self) -> float:
        return self.pinball_low + self.pinball_high


def _labels(bars: list[Bar], t: int) -> tuple[float, float]:
    f = float(bars[t + 1].adj_close) / float(bars[t + 1].close)
    base = float(bars[t].adj_close)
    return float(bars[t + 1].low) * f / base - 1, float(bars[t + 1].high) * f / base - 1


def rolling_eval(bars: list[Bar], predictor: Callable[[list[Bar]], Prediction], *, start: int, step: int = 1, alpha_low: float = 0.10,
                 alpha_high: float = 0.90) -> EvalResult:
    """从 index=start 起每隔 step 个交易日做一次「截至 t 预测 t+1」，用 t+1 的真实高低揭示标签。"""
    pl = ph = 0.0
    cl = ch = n = skipped = 0
    for t in range(start, len(bars) - 1, step):
        try:
            pr = predictor(bars[: t + 1])
        except ForecastUnavailable:
            skipped += 1
            continue
        yl, yh = _labels(bars, t)
        pl += pinball(yl, float(pr.y_low), alpha_low)
        ph += pinball(yh, float(pr.y_high), alpha_high)
        cl += yl < float(pr.y_low)
        ch += yh > float(pr.y_high)
        n += 1
    if n == 0:
        raise ForecastUnavailable("没有可评估的起点")
    return EvalResult(n, pl / n, ph / n, cl / n, ch / n, skipped)


def improvement(base: EvalResult, cand: EvalResult) -> float:
    """候选相对基线的 raw pinball 改善比例（正＝候选更好）。"""
    return (base.total - cand.total) / base.total


def meets_gate(per_name: dict[str, float], *, min_improvement: float = 0.05, min_names_improved: int | None = None) -> dict:
    """per_name：{标的: 改善比例}。门槛：等权平均改善 ≥ min_improvement，且至少 min_names_improved（默认 ⌈2n/3⌉）个标的改善 > 0。"""
    n = len(per_name)
    need = min_names_improved if min_names_improved is not None else -(-2 * n // 3)
    mean = sum(per_name.values()) / n if n else float("nan")
    improved = sum(v > 0 for v in per_name.values())
    return {"mean_improvement": mean, "names_improved": improved, "names_needed": need, "n_names": n,
            "pass": bool(n and mean >= min_improvement and improved >= need)}
