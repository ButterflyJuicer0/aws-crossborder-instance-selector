from crossborder_selector.models import (
    Candidate, IspProbe, ProbeResult, SourceResult, ReputationResult,
)


def test_isp_probe_loss():
    assert IspProbe("telecom", 10, 8).loss == 0.2
    assert IspProbe("telecom", 0, 0).loss == 1.0


def test_probe_result_ok_requires_probes_and_no_error():
    assert ProbeResult("reverse", [IspProbe("telecom", 1, 1, 10.0)]).ok is True
    assert ProbeResult("reverse", []).ok is False
    assert ProbeResult("reverse", [IspProbe("telecom", 1, 1)], error="boom").ok is False


def test_reputation_any_listed():
    rr = ReputationResult("1.2.3.4", [SourceResult("a", False), SourceResult("b", True)], 50.0)
    assert rr.any_listed is True


def test_candidate_defaults():
    c = Candidate("i-1", "1.2.3.4")
    assert c.prefix == "" and c.round == 0 and c.ssm_online is True
