import pytest
from crossborder_selector.models import Candidate, IspProbe, ProbeResult, ReputationResult, SourceResult
from crossborder_selector.scoring import (latency_factor, probe_score, isp_scores_for, backend_score,
                                          score_candidate, rank)

W = {"backends": {"reverse": 0.5, "globalping": 0.2, "ripeatlas": 0.15, "itdog": 0.15},
     "isps": {"telecom": 0.34, "unicom": 0.33, "mobile": 0.33}, "lat_good_ms": 60, "lat_bad_ms": 300}
CLEAN = ReputationResult("1.1.1.1", [SourceResult("dnsbl", False)], 100.0)
DIRTY = ReputationResult("1.1.1.1", [SourceResult("dnsbl", True)], 50.0)
C = Candidate("i-1", "1.1.1.1")


def rev(t=50.0, u=50.0, m=50.0, rx=4):
    return ProbeResult("reverse", [IspProbe("telecom", 4, rx, t), IspProbe("unicom", 4, rx, u), IspProbe("mobile", 4, rx, m)])


def test_latency_factor_boundaries():
    assert latency_factor(30, 60, 300) == 1.0
    assert latency_factor(60, 60, 300) == 1.0
    assert latency_factor(180, 60, 300) == pytest.approx(0.5)
    assert latency_factor(300, 60, 300) == 0.0
    assert latency_factor(None, 60, 300) == 0.0


def test_probe_score_combines_loss_and_latency():
    assert probe_score(IspProbe("telecom", 10, 10, 60), 60, 300) == 100.0
    assert probe_score(IspProbe("telecom", 10, 5, 180), 60, 300) == pytest.approx(25.0)


def test_isp_scores_average_multiple_targets():
    pr = ProbeResult("reverse", [IspProbe("telecom", 4, 4, 60), IspProbe("telecom", 4, 4, 300)])
    assert isp_scores_for(pr, 60, 300) == {"telecom": pytest.approx(50.0)}


def test_backend_score_weights_three_isps_else_equal_mean():
    assert backend_score({"telecom": 100, "unicom": 100, "mobile": 0}, W["isps"]) == pytest.approx(67.0)
    assert backend_score({"HK": 80, "TW": 40}, W["isps"]) == pytest.approx(60.0)


def test_veto_reputation():
    s = score_candidate(C, DIRTY, [rev()], W, 1, True)
    assert s.qualified is False and s.veto_reason == "reputation"


def test_veto_reverse_unreachable():
    s = score_candidate(C, CLEAN, [rev(rx=0)], W, 1, True)
    assert s.qualified is False and s.veto_reason == "reverse_unreachable"


def test_veto_min_backends():
    s = score_candidate(C, CLEAN, [ProbeResult("reverse", [], "ssm offline")], W, 1, False)
    assert s.qualified is False and s.veto_reason == "min_backends"


def test_composite_renormalizes_over_valid_backends():
    gp = ProbeResult("globalping", [IspProbe("HK", 4, 4, 60)])
    s = score_candidate(C, CLEAN, [rev(60, 60, 60), gp, ProbeResult("ripeatlas", [], "no key")], W, 1, True)
    assert s.qualified is True
    assert s.composite == pytest.approx(100.0)
    assert set(s.backend_scores) == {"reverse", "globalping"}
    s2 = score_candidate(C, CLEAN, [rev(60, 60, 60), ProbeResult("globalping", [IspProbe("HK", 4, 0, None)])], W, 1, True)
    # 实现对 composite 取 round(_,2)=71.43，与未取整期望 71.4286 差属取整噪声，放宽容差
    assert s2.composite == pytest.approx(100 * 0.5 / 0.7, abs=0.01)


def test_rank_order():
    a = score_candidate(Candidate("a", "1.1.1.1"), CLEAN, [rev(60, 60, 60)], W, 1, True)
    b = score_candidate(Candidate("b", "2.2.2.2"), CLEAN, [rev(180, 180, 180)], W, 1, True)
    v = score_candidate(Candidate("v", "3.3.3.3"), DIRTY, [rev()], W, 1, True)
    assert [s.candidate.instance_id for s in rank([b, v, a])] == ["a", "b", "v"]


# ---- P95 / 抖动 / prefix 历史 / agent 否决 ----

def test_probe_score_prefers_p95_over_mean_when_available():
    # 均值 70 很好看，但 P95 500 ms 已超过 lat_bad，得分应为 0
    p = IspProbe("telecom", 10, 10, 70.0, p95_rtt_ms=500.0)
    assert probe_score(p, 60, 300) == 0.0
    # 均值 95 / P95 110：按 P95 线性衰减 (300-110)/(300-60)
    q = IspProbe("telecom", 10, 10, 95.0, p95_rtt_ms=110.0)
    assert probe_score(q, 60, 300) == pytest.approx(100 * (300 - 110) / 240)


def test_probe_score_applies_jitter_penalty():
    steady = IspProbe("telecom", 10, 10, 50.0, p95_rtt_ms=55.0, jitter_ms=2.0)
    shaky = IspProbe("telecom", 10, 10, 50.0, p95_rtt_ms=55.0, jitter_ms=80.0)
    assert probe_score(steady, 60, 300, jitter_bad_ms=50, jitter_penalty=0.3) == pytest.approx(100 * (1 - 0.3 * 2 / 50))
    # 抖动超过 jitter_bad_ms 时扣满 penalty，不再继续扣
    assert probe_score(shaky, 60, 300, jitter_bad_ms=50, jitter_penalty=0.3) == pytest.approx(70.0)
    # 没有抖动数据不扣分
    assert probe_score(IspProbe("telecom", 10, 10, 50.0), 60, 300, jitter_bad_ms=50, jitter_penalty=0.3) == 100.0


def test_score_candidate_blends_prefix_history():
    w = {**W, "prefix_history": 0.3, "prefix_min_samples": 3}
    c = Candidate("i-1", "1.1.1.1", prefix="18.162.0.0/16")
    hist = {"18.162.0.0/16": {"samples": 10, "mean_composite": 40.0, "best_composite": 90.0}}
    s = score_candidate(c, CLEAN, [rev()], w, 1, True, prefix_history=hist)
    assert s.instant_composite == 100.0
    assert s.prefix_history_score == 40.0
    assert s.composite == pytest.approx(0.7 * 100.0 + 0.3 * 40.0)


def test_prefix_history_ignored_below_min_samples_or_unknown_prefix():
    w = {**W, "prefix_history": 0.3, "prefix_min_samples": 3}
    thin = {"18.162.0.0/16": {"samples": 2, "mean_composite": 40.0, "best_composite": 90.0}}
    s = score_candidate(Candidate("i-1", "1.1.1.1", prefix="18.162.0.0/16"), CLEAN, [rev()], w, 1, True, prefix_history=thin)
    assert s.composite == 100.0 and s.prefix_history_score is None
    s2 = score_candidate(Candidate("i-2", "2.2.2.2", prefix="54.0.0.0/8"), CLEAN, [rev()], w, 1, True, prefix_history=thin)
    assert s2.composite == 100.0 and s2.prefix_history_score is None


def test_veto_agent_unavailable_when_agent_enabled_but_missing():
    s = score_candidate(C, CLEAN, [rev()], W, 1, True, agent_enabled=True)
    assert not s.qualified and s.veto_reason == "agent_unavailable"
    agent = ProbeResult("agent", [IspProbe("telecom", 10, 10, 80.0, p95_rtt_ms=90.0, method="tcp")])
    ok = score_candidate(C, CLEAN, [rev(), agent], {**W, "backends": {**W["backends"], "agent": 0.4}}, 1, True, agent_enabled=True)
    assert ok.qualified and ok.veto_reason == ""
