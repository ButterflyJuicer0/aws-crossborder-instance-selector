import json
import pytest
from crossborder_selector.models import Candidate
from crossborder_selector.probes.agent import build_agent_script, parse_agent_output, AgentBackend, MARKER

CFG = {"enabled": True, "profile": "cn", "region": "cn-north-1",
       "instances": {"i-bj-telecom": "telecom", "i-bj-unicom": "unicom"},
       "ping_count": 4, "tcp_ports": [443, 22], "tcp_count": 3, "timeout_s": 120}


def test_build_agent_script_covers_every_ip_port_and_marker():
    s = build_agent_script(["1.1.1.1", "2.2.2.2"], ping_count=4, tcp_ports=[443, 22], tcp_count=3)
    for ip in ("1.1.1.1", "2.2.2.2"):
        assert f'probe_ip "{ip}"' in s
    assert "443" in s and "22" in s and "/dev/tcp/" in s and MARKER in s
    assert 'ping -c "$2"' in s or "ping -c" in s
    assert "set -e" not in s  # 单目标失败不能中断


def test_parse_agent_output_computes_p95_jitter_and_tcp_stats():
    payload = [
        {"ip": "1.1.1.1",
         "ping": {"sent": 4, "rtts": [80.0, 82.0, 79.0, 300.0]},
         "tcp": [{"port": 443, "attempts": 3, "connect_ms": [90, 95, 400]},
                 {"port": 22, "attempts": 3, "connect_ms": []}]},
    ]
    out = parse_agent_output("noise\n" + MARKER + json.dumps(payload) + "\n", isp="telecom")
    probes = out["1.1.1.1"]
    ping = next(p for p in probes if p.method == "ping")
    assert ping.isp == "telecom" and ping.sent == 4 and ping.received == 4
    assert ping.median_rtt_ms == pytest.approx(135.25) and ping.p95_rtt_ms == 300.0
    assert ping.jitter_ms == pytest.approx((2 + 3 + 221) / 3)
    tcp443 = next(p for p in probes if p.method == "tcp" and p.target.endswith(":443"))
    assert tcp443.sent == 3 and tcp443.received == 3 and tcp443.p95_rtt_ms == 400
    tcp22 = next(p for p in probes if p.method == "tcp" and p.target.endswith(":22"))
    assert tcp22.sent == 3 and tcp22.received == 0 and tcp22.median_rtt_ms is None


def test_parse_agent_output_without_marker_raises():
    with pytest.raises(ValueError):
        parse_agent_output("garbage", isp="telecom")


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []

    def run_script(self, instance_id, script, timeout_s=120):
        self.calls.append((instance_id, script, timeout_s))
        return self.outcomes[instance_id]


def _payload(ip, rtts):
    return [{"ip": ip, "ping": {"sent": len(rtts), "rtts": rtts}, "tcp": [{"port": 443, "attempts": 2, "connect_ms": [50, 60]}]}]


def test_agent_backend_merges_isps_and_tolerates_single_agent_failure():
    good_t = MARKER + json.dumps(_payload("1.1.1.1", [80, 82]) + _payload("2.2.2.2", [120, 130]))
    runner = FakeRunner({"i-bj-telecom": ("Success", good_t), "i-bj-unicom": ("Failed", "boom")})
    b = AgentBackend(runner, CFG)
    out = b.probe([Candidate("i-a", "1.1.1.1"), Candidate("i-b", "2.2.2.2")])
    # 每个 agent 实例只下发一条命令，脚本里包含全部候选 IP
    assert sorted(c[0] for c in runner.calls) == ["i-bj-telecom", "i-bj-unicom"]
    assert "1.1.1.1" in runner.calls[0][1] and "2.2.2.2" in runner.calls[0][1]
    pr = out["1.1.1.1"]
    assert pr.ok and {p.isp for p in pr.probes} == {"telecom"}
    assert "unicom" in pr.error or pr.error == ""  # 单 agent 失败记录为警告，不影响 ok
    assert out["2.2.2.2"].ok and out["2.2.2.2"].probes[0].median_rtt_ms == 125.0


def test_agent_backend_all_agents_failing_yields_error():
    runner = FakeRunner({"i-bj-telecom": ("TimedOut", ""), "i-bj-unicom": ("Failed", "boom")})
    out = AgentBackend(runner, CFG).probe([Candidate("i-a", "1.1.1.1")])
    assert not out["1.1.1.1"].ok and "TimedOut" in out["1.1.1.1"].error and "Failed" in out["1.1.1.1"].error


def test_agent_backend_name_and_isp_label_passthrough():
    cfg = {**CFG, "instances": {"i-any": "cn-north-1"}}
    runner = FakeRunner({"i-any": ("Success", MARKER + json.dumps(_payload("9.9.9.9", [10, 12])))})
    b = AgentBackend(runner, cfg)
    assert b.name == "agent"
    assert b.probe([Candidate("i-x", "9.9.9.9")])["9.9.9.9"].probes[0].isp == "cn-north-1"
