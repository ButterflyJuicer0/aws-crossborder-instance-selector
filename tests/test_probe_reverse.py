import json
from crossborder_selector.models import Candidate
from crossborder_selector.probes.reverse import build_script, parse_output, ReverseBackend

TARGETS = {"telecom": ["114.114.114.114"], "unicom": ["123.123.123.123"], "mobile": ["221.130.33.52"]}
CFG = {"enabled": True, "ping_count": 4, "tcping_count": 2, "tcping_port": 443, "timeout_s": 60, "targets": TARGETS}


def test_build_script_mentions_every_target_and_marker():
    s = build_script(TARGETS, 4, 2, 443)
    for t in ("114.114.114.114", "123.123.123.123", "221.130.33.52"):
        assert t in s
    # 脚本用 ping -c "$3"，计数经 probe_ping 位置参数传入，故不会出现字面量 "ping -c 4"
    assert 'ping -c "$3"' in s and 'probe_ping "telecom" "114.114.114.114" 4' in s
    assert "/dev/tcp/" in s and "CROSSBORDER_JSON:" in s
    assert "set -e" not in s  # 单目标失败不能中断脚本


def test_parse_output():
    payload = [
        {"isp": "telecom", "target": "114.114.114.114", "method": "ping", "sent": 4, "received": 4, "avg_ms": 45.2},
        {"isp": "mobile", "target": "221.130.33.52:443", "method": "tcp", "sent": 2, "received": 0, "avg_ms": None},
    ]
    probes = parse_output("noise\nCROSSBORDER_JSON:" + json.dumps(payload) + "\n")
    assert len(probes) == 2
    assert probes[0].isp == "telecom" and probes[0].median_rtt_ms == 45.2 and probes[0].loss == 0.0
    assert probes[1].received == 0 and probes[1].median_rtt_ms is None and probes[1].method == "tcp"


def test_parse_output_without_marker_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_output("garbage")


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []
    def run_script(self, instance_id, script, timeout_s=120):
        self.calls.append((instance_id, timeout_s))
        return self.outcomes[instance_id]


def test_reverse_backend_maps_statuses():
    good = "CROSSBORDER_JSON:" + json.dumps([{"isp": "telecom", "target": "x", "method": "ping", "sent": 4, "received": 3, "avg_ms": 80}])
    runner = FakeRunner({"i-ok": ("Success", good), "i-fail": ("Failed", "boom"), "i-bad": ("Success", "no marker")})
    b = ReverseBackend(runner, CFG)
    cands = [Candidate("i-ok", "1.1.1.1"), Candidate("i-fail", "2.2.2.2"),
             Candidate("i-bad", "3.3.3.3"), Candidate("i-off", "4.4.4.4", ssm_online=False)]
    out = b.probe(cands)
    assert out["1.1.1.1"].ok and out["1.1.1.1"].probes[0].received == 3
    assert "Failed" in out["2.2.2.2"].error
    assert "marker" in out["3.3.3.3"].error.lower() or "json" in out["3.3.3.3"].error.lower()
    assert out["4.4.4.4"].error == "ssm offline"
    assert all(t == 60 for _, t in runner.calls) and len(runner.calls) == 3
