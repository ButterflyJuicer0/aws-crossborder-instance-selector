"""单文件 agent（agent/crossborder_agent.py）：只依赖标准库，可在任意机器上运行。"""
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "crossborder_agent.py"


@pytest.fixture(scope="module")
def agent():
    spec = importlib.util.spec_from_file_location("crossborder_agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_agent_file_is_stdlib_only(agent):
    src = AGENT_PATH.read_text()
    assert "import requests" not in src and "import yaml" not in src
    assert "boto3" in src  # 仅在 S3 模式按需导入
    assert src.lstrip().startswith("#!/usr/bin/env python3")


LINUX_PING = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=53 time=103 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=53 time=2.45 ms
64 bytes from 1.1.1.1: icmp_seq=4 ttl=53 time=105.2 ms

--- 1.1.1.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 606ms
"""
MACOS_PING = """PING 1.1.1.1 (1.1.1.1): 56 data bytes
64 bytes from 1.1.1.1: icmp_seq=0 ttl=56 time=12.345 ms
64 bytes from 1.1.1.1: icmp_seq=1 ttl=56 time=11.9 ms
Request timeout for icmp_seq 2

--- 1.1.1.1 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 11.900/12.123/12.345/0.223 ms
"""


def test_parse_ping_output_linux_and_macos(agent):
    assert agent.parse_ping_output(LINUX_PING) == [103.0, 2.45, 105.2]
    assert agent.parse_ping_output(MACOS_PING) == [12.345, 11.9]
    assert agent.parse_ping_output("") == []


def test_ping_command_for_platform(agent):
    linux = agent.ping_command("1.1.1.1", 4, platform="linux")
    mac = agent.ping_command("1.1.1.1", 4, platform="darwin")
    assert linux[:2] == ["ping", "-c"] and "1.1.1.1" in linux and "-W" in linux
    assert mac[:2] == ["ping", "-c"] and "1.1.1.1" in mac and "-W" not in mac  # macOS 的 -W 单位/语义不同


def test_tcp_connect_times_real_socket(agent):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen(5)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: [srv.accept() for _ in range(3)], daemon=True).start()
    ok = agent.tcp_connect_times("127.0.0.1", port, attempts=3, timeout_s=2)
    assert len(ok) == 3 and all(0 <= ms < 1000 for ms in ok)
    srv.close()
    closed = socket.socket(); closed.bind(("127.0.0.1", 0)); closed_port = closed.getsockname()[1]; closed.close()
    assert agent.tcp_connect_times("127.0.0.1", closed_port, attempts=2, timeout_s=1) == []


def test_probe_targets_builds_shared_payload(agent, monkeypatch):
    monkeypatch.setattr(agent, "run_ping", lambda ip, count: [80.0, 82.0])
    monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [50.0, 60.0] if port == 443 else [])
    items = agent.probe_targets(["1.1.1.1"], ping_count=2, tcp_ports=[443, 22], tcp_count=2)
    assert items == [{"ip": "1.1.1.1", "ping": {"sent": 2, "rtts": [80.0, 82.0]},
                      "tcp": [{"port": 443, "attempts": 2, "connect_ms": [50.0, 60.0]},
                              {"port": 22, "attempts": 2, "connect_ms": []}]}]


def test_once_mode_prints_marker_line(agent, monkeypatch, capsys):
    monkeypatch.setattr(agent, "run_ping", lambda ip, count: [10.0])
    monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [5.0])
    rc = agent.main(["once", "--targets", "1.1.1.1,2.2.2.2", "--ports", "443", "--ping-count", "1", "--tcp-count", "1"])
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("CROSSBORDER_AGENT_JSON:"))
    payload = json.loads(line[len("CROSSBORDER_AGENT_JSON:"):])
    assert rc == 0 and [i["ip"] for i in payload] == ["1.1.1.1", "2.2.2.2"]


def test_serve_http_one_cycle_claims_and_submits(agent, monkeypatch):
    from crossborder_selector.probes.agent_transport import AgentJobStore, AgentHttpServer
    store = AgentJobStore()
    srv = AgentHttpServer(store, "127.0.0.1", 0, token="tok")
    srv.start()
    try:
        job = store.publish(["1.1.1.1"], 3, [443], 2, ttl_s=60)
        monkeypatch.setattr(agent, "run_ping", lambda ip, count: [30.0, 31.0, 29.0])
        monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [40.0, 41.0])
        rc = agent.main(["serve", "--server", f"http://127.0.0.1:{srv.port}", "--token", "tok",
                        "--agent-id", "laptop", "--isp", "telecom", "--once"])
        assert rc == 0
        res = store.collect(job["job_id"], 1, 0)
        assert res["laptop"]["isp"] == "telecom" and res["laptop"]["items"][0]["ping"]["rtts"] == [30.0, 31.0, 29.0]
        assert res["laptop"]["items"][0]["tcp"][0]["connect_ms"] == [40.0, 41.0]
    finally:
        srv.stop()


def test_serve_http_once_with_no_job_exits_zero(agent):
    from crossborder_selector.probes.agent_transport import AgentJobStore, AgentHttpServer
    srv = AgentHttpServer(AgentJobStore(), "127.0.0.1", 0, token="tok")
    srv.start()
    try:
        rc = agent.main(["serve", "--server", f"http://127.0.0.1:{srv.port}", "--token", "tok",
                        "--agent-id", "laptop", "--isp", "telecom", "--once"])
        assert rc == 0
    finally:
        srv.stop()


def test_serve_s3_one_cycle(agent, monkeypatch):
    import boto3
    from moto import mock_aws
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="xb-agent")
        from crossborder_selector.probes.agent_transport import S3AgentBroker
        broker = S3AgentBroker(s3, "xb-agent", "p")
        job = broker.publish(["9.9.9.9"], 2, [443], 1, ttl_s=60)
        monkeypatch.setattr(agent, "run_ping", lambda ip, count: [5.0, 6.0])
        monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [7.0])
        monkeypatch.setattr(agent, "make_s3_client", lambda profile, region: s3)
        rc = agent.main(["serve", "--s3", "s3://xb-agent/p", "--agent-id", "cn-box", "--isp", "mobile", "--once"])
        assert rc == 0
        res = broker.collect(job["job_id"], 1, 0)
        assert res["cn-box"]["isp"] == "mobile" and res["cn-box"]["items"][0]["ip"] == "9.9.9.9"


# ---- agent 自报公网出口 IP，供选择器自动生成拨测来源 ----

def test_detect_public_ip_uses_fetcher_and_caches(agent):
    calls = []
    def fetcher(url, timeout):
        calls.append(url); return b" 203.0.113.7\n"
    assert agent.detect_public_ip(fetcher=fetcher) == "203.0.113.7"
    assert agent.detect_public_ip(fetcher=fetcher) == "203.0.113.7" and len(calls) == 1  # 缓存
    agent.detect_public_ip.cache_clear()
    def broken(url, timeout): raise OSError("offline")
    assert agent.detect_public_ip(fetcher=broken) is None
    agent.detect_public_ip.cache_clear()
    def garbage(url, timeout): return b"<html>"
    assert agent.detect_public_ip(fetcher=garbage) is None
    agent.detect_public_ip.cache_clear()


def test_http_transport_reports_public_ip_on_claim_and_submit(agent, monkeypatch):
    from crossborder_selector.probes.agent_transport import AgentJobStore, AgentHttpServer
    store = AgentJobStore()
    srv = AgentHttpServer(store, "127.0.0.1", 0, token="tok").start()
    try:
        job = store.publish(["1.1.1.1"], 2, [443], 1, ttl_s=60)
        monkeypatch.setattr(agent, "detect_public_ip", lambda fetcher=None: "203.0.113.7")
        monkeypatch.setattr(agent, "run_ping", lambda ip, count: [5.0])
        monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [6.0])
        rc = agent.main(["serve", "--server", f"http://127.0.0.1:{srv.port}", "--token", "tok",
                        "--agent-id", "lap", "--isp", "telecom", "--once"])
        assert rc == 0
        reg = store.registry()["lap"]
        assert reg["ip"] == "127.0.0.1" and reg["public_ip"] == "203.0.113.7"
        assert store.collect(job["job_id"], 1, 0)["lap"]["public_ip"] == "203.0.113.7"
    finally:
        srv.stop()


def test_s3_transport_writes_heartbeat_with_public_ip(agent, monkeypatch):
    import boto3, json
    from moto import mock_aws
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1"); s3.create_bucket(Bucket="xb-agent")
        monkeypatch.setattr(agent, "detect_public_ip", lambda fetcher=None: "198.51.100.9")
        monkeypatch.setattr(agent, "make_s3_client", lambda profile, region: s3)
        rc = agent.main(["serve", "--s3", "s3://xb-agent/p", "--agent-id", "cn-box", "--isp", "mobile", "--once"])
        assert rc == 0
        body = json.loads(s3.get_object(Bucket="xb-agent", Key="p/registry/cn-box.json")["Body"].read())
        assert body["agent_id"] == "cn-box" and body["isp"] == "mobile" and body["public_ip"] == "198.51.100.9"
        assert body["last_seen"] > 0


def test_probe_targets_runs_targets_concurrently_and_keeps_order(agent, monkeypatch):
    import time as _t
    def slow_ping(ip, count):
        _t.sleep(0.3); return [float(ip.split(".")[-1])]
    monkeypatch.setattr(agent, "run_ping", slow_ping)
    monkeypatch.setattr(agent, "tcp_connect_times", lambda ip, port, attempts, timeout_s=3.0: [1.0])
    ips = [f"10.0.0.{i}" for i in range(1, 9)]
    t0 = _t.time()
    items = agent.probe_targets(ips, ping_count=1, tcp_ports=[443], tcp_count=1)
    elapsed = _t.time() - t0
    assert [i["ip"] for i in items] == ips                       # 顺序不变
    assert [i["ping"]["rtts"] for i in items] == [[float(n)] for n in range(1, 9)]
    assert elapsed < 1.5, f"8 个目标应并发完成，实际 {elapsed:.1f}s（串行约 2.4s）"
