"""逐包时延的分位数与抖动。所有函数对空序列返回 None，不抛异常。"""
import math
from statistics import mean


def percentile(values, pct) -> float | None:
    """最近秩法（nearest-rank）分位数；pct 取 0–100。"""
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    rank = max(1, math.ceil(pct / 100.0 * len(vals)))
    return vals[min(rank, len(vals)) - 1]


def jitter(values) -> float | None:
    """相邻样本绝对差的平均值（RFC 3550 的简化形式）。单个样本抖动为 0。"""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    if len(vals) == 1:
        return 0.0
    return mean(abs(b - a) for a, b in zip(vals, vals[1:]))


def rtt_summary(values) -> dict:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return {"avg": None, "p95": None, "jitter": None}
    return {"avg": mean(vals), "p95": percentile(vals, 95), "jitter": jitter(vals)}
