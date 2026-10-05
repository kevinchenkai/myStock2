"""配对差与统计（方案 §6A.5、§6A.7；纯 Python，无 numpy 依赖）。

- `paired_diff`：只在两条线该日均为 OK 的日期配对，Δ_t=r_A,t−r_B,t；报告配对覆盖率。
- `block_bootstrap_ci`：按时间块的成组重采样（块内保持相邻日相关）；**滚动区间只作描述，不触发晋级**。
- `required_days`：功效估算 n ≈ ((z_α/2 + z_β)·σ/μ)²（§2.3，示意，非保证）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal

from mystock2.scoreboard.metrics import daily_returns
from mystock2.scoreboard.types import DayResult


@dataclass
class PairedDiff:
    deltas: dict                     # date -> Δ_t（Decimal）
    coverage: Decimal | None         # 配对日 / 两线计划日的较小者
    cumulative: Decimal              # ΣΔ_t
    mean: Decimal | None
    n: int


def ambiguous_dates(*runs: list[DayResult]) -> set:
    return {r.date for res in runs for r in res if "ambiguous_bar" in r.flags}


def paired_diff(a: list[DayResult], b: list[DayResult], e0: Decimal, *, exclude_dates: set | None = None) -> PairedDiff:
    """exclude_dates：敏感性用（如剔除双边歧义日）；排除的日期不进入配对，覆盖率分母不变。"""
    ra, rb = daily_returns(a, e0), daily_returns(b, e0)
    common = sorted((set(ra) & set(rb)) - (exclude_dates or set()))
    deltas = {d: ra[d] - rb[d] for d in common}
    planned = min(len(a), len(b))
    return PairedDiff(deltas, Decimal(len(common)) / Decimal(planned) if planned else None, sum(deltas.values(), Decimal(0)),
                      (sum(deltas.values(), Decimal(0)) / len(deltas)) if deltas else None, len(deltas))


def block_bootstrap_ci(values: list[float], *, block_len: int = 5, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict:
    """均值的时间块自助法置信区间。样本不足以成块时返回 None 区间（不给确定性结论）。"""
    n = len(values)
    if n < 2 * block_len:
        return {"n": n, "mean": (sum(values) / n) if n else None, "lo": None, "hi": None, "note": "样本不足以做块自助法"}
    rnd = random.Random(seed)
    blocks = n - block_len + 1
    k = math.ceil(n / block_len)
    means = []
    for _ in range(n_boot):
        s = []
        for _ in range(k):
            i = rnd.randrange(blocks)
            s.extend(values[i:i + block_len])
        s = s[:n]
        means.append(sum(s) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return {"n": n, "mean": sum(values) / n, "lo": lo, "hi": hi, "note": "滚动区间仅作描述，不触发晋级"}


def required_days(mu: float, sigma: float, z_alpha: float = 1.96, z_beta: float = 0.84) -> float:
    """检出日均优势 mu 所需交易日数（独立同分布近似；真实配对差有相关与厚尾，仅示意）。"""
    if mu == 0:
        return math.inf
    return ((z_alpha + z_beta) * sigma / mu) ** 2
