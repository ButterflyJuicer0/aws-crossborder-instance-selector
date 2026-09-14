import pytest
from crossborder_selector.stats import percentile, jitter, rtt_summary


def test_percentile_nearest_rank():
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(vals, 50) == 50
    assert percentile(vals, 95) == 100
    assert percentile([42.0], 95) == 42.0


def test_percentile_empty_is_none():
    assert percentile([], 95) is None


def test_jitter_is_mean_absolute_successive_difference():
    # |20-10| + |15-20| + |35-15| = 10 + 5 + 20 = 35 / 3
    assert jitter([10, 20, 15, 35]) == pytest.approx(35 / 3)
    assert jitter([50]) == 0.0
    assert jitter([]) is None


def test_rtt_summary_returns_avg_p95_jitter():
    s = rtt_summary([2.45, 2.16, 4.23, 2.26])
    assert s["avg"] == pytest.approx(2.775, abs=1e-3)
    assert s["p95"] == 4.23
    assert s["jitter"] == pytest.approx((0.29 + 2.07 + 1.97) / 3, abs=1e-3)
    assert rtt_summary([]) == {"avg": None, "p95": None, "jitter": None}
