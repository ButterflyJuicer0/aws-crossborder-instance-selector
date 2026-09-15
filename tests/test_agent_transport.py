"""agent 远程传输：任务信箱（内存/HTTP/S3）与 RemoteAgentBackend。"""
import json
import threading
import time
import urllib.request
import urllib.error

import boto3
import pytest
from moto import mock_aws

from crossborder_selector.models import Candidate
from crossborder_selector.probes.agent_transport import (AgentJobStore, AgentHttpServer, S3AgentBroker,
                                                         RemoteAgentBackend)

CFG = {"enabled": True, "transport": "http", "ping_count": 4, "tcp_ports": [443], "tcp_count": 3,
       "timeout_s": 5, "min_agents": 1, "instances": {},
       "http": {"listen": "127.0.0.1:0", "token": "secret"}, "s3": {"bucket": "", "prefix": "crossborder-agent"}}


def _items(ip, rtts, connect_ms=(50, 60)):
    return [{"ip": ip, "ping": {"sent": len(rtts), "rtts": list(rtts)},
             "tcp": [{"port": 443, "attempts": len(connect_ms), "connect_ms": list(connect_ms)}]}]


# ---------- 内存信箱 ----------

def test_store_publish_claim_submit_collect():
    store = AgentJobStore(clock=lambda: 0.0)
    job = store.publish(["1.1.1.1", "2.2.2.2"], ping_count=4, tcp_ports=[443], tcp_count=3, ttl_s=60)
    claimed = store.claim("agent-bj", isp="telecom")
    assert claimed["job_id"] == job["job_id"] and claimed["ips"] == ["1.1.1.1", "2.2.2.2"]
    assert claimed["ping_count"] == 4 and claimed["tcp_ports"] == [443] and claimed["tcp_count"] == 3
    # 同一 agent 不会重复领到同一任务；另一 agent 可以领
    assert store.claim("agent-bj", isp="telecom") is None
    assert store.claim("agent-sh", isp="unicom")["job_id"] == job["job_id"]
    store.submit(job["job_id"], "agent-bj", "telecom", _items("1.1.1.1", [80, 82]) + _items("2.2.2.2", [120, 130]))
    results = store.collect(job["job_id"], min_agents=1, timeout_s=0)
    assert set(results) == {"agent-bj"} and results["agent-bj"]["isp"] == "telecom"
    assert results["agent-bj"]["items"][0]["ip"] == "1.1.1.1"
    assert store.registry()["agent-bj"]["isp"] == "telecom"


def test_store_collect_waits_for_min_agents_then_returns():
    store = AgentJobStore()  # 真实时钟：验证并发提交能在超时前被收齐
    job = store.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)

    def late_submit():
        time.sleep(0.05)
        store.submit(job["job_id"], "a1", "telecom", _items("1.1.1.1", [10]))
        store.submit(job["job_id"], "a2", "unicom", _items("1.1.1.1", [20]))
    threading.Thread(target=late_submit).start()
    t0 = time.time()
    results = store.collect(job["job_id"], min_agents=2, timeout_s=5, poll_s=0.01)
    assert set(results) == {"a1", "a2"} and time.time() - t0 < 4


def test_store_collect_times_out_with_partial_results():
    t = [0.0]
    store = AgentJobStore(clock=lambda: t[0], sleeper=lambda s: t.__setitem__(0, t[0] + s))
    job = store.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)
    store.submit(job["job_id"], "a1", "telecom", _items("1.1.1.1", [10]))
    results = store.collect(job["job_id"], min_agents=3, timeout_s=5, poll_s=1)
    assert set(results) == {"a1"} and t[0] >= 5


def test_store_expires_old_jobs_and_ignores_unknown_submissions():
    t = [0.0]
    store = AgentJobStore(clock=lambda: t[0])
    job = store.publish(["1.1.1.1"], 4, [443], 3, ttl_s=10)
    t[0] = 11
    assert store.claim("a1", isp="telecom") is None
    with pytest.raises(KeyError):
        store.submit("job-does-not-exist", "a1", "telecom", [])
    assert store.collect(job["job_id"], 1, 0) == {}


# ---------- HTTP 传输 ----------

@pytest.fixture
def http_server():
    store = AgentJobStore()
    srv = AgentHttpServer(store, "127.0.0.1", 0, token="secret")
    srv.start()
    yield store, f"http://127.0.0.1:{srv.port}"
    srv.stop()


def _get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read() or b"null")


def _post(url, body, token=None):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read() or b"null")


def test_http_requires_token_and_serves_jobs_and_results(http_server):
    store, base = http_server
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{base}/api/agent/jobs?agent_id=a1&isp=telecom")
    assert ei.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{base}/api/agent/jobs?agent_id=a1&isp=telecom", token="wrong")
    assert ei.value.code == 401
    # 无任务时返回 204
    status, body = _get(f"{base}/api/agent/jobs?agent_id=a1&isp=telecom", token="secret")
    assert status == 204
    job = store.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)
    status, body = _get(f"{base}/api/agent/jobs?agent_id=a1&isp=telecom", token="secret")
    assert status == 200 and body["job_id"] == job["job_id"] and body["ips"] == ["1.1.1.1"]
    status, body = _post(f"{base}/api/agent/results",
                         {"job_id": job["job_id"], "agent_id": "a1", "isp": "telecom", "items": _items("1.1.1.1", [10, 12])},
                         token="secret")
    assert status == 200 and body["accepted"] is True
    assert store.collect(job["job_id"], 1, 0)["a1"]["items"][0]["ping"]["rtts"] == [10, 12]
    # 未知任务返回 404，畸形 JSON 返回 400
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(f"{base}/api/agent/results", {"job_id": "nope", "agent_id": "a1", "isp": "x", "items": []}, token="secret")
    assert ei.value.code == 404
    status, body = _get(f"{base}/api/agent/registry", token="secret")
    assert status == 200 and body["a1"]["isp"] == "telecom"


# ---------- S3 传输 ----------

@mock_aws
def test_s3_broker_publish_and_collect_roundtrip():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="xb-agent")
    t = [0.0]
    broker = S3AgentBroker(s3, "xb-agent", "crossborder-agent", clock=lambda: t[0], sleeper=lambda s: t.__setitem__(0, t[0] + s))
    job = broker.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="xb-agent", Prefix="crossborder-agent/jobs/")["Contents"]]
    assert keys == [f"crossborder-agent/jobs/{job['job_id']}.json"]
    # agent 侧写入结果
    s3.put_object(Bucket="xb-agent", Key=f"crossborder-agent/results/{job['job_id']}/agent-a.json",
                  Body=json.dumps({"agent_id": "agent-a", "isp": "mobile", "items": _items("1.1.1.1", [90, 95])}).encode())
    results = broker.collect(job["job_id"], min_agents=1, timeout_s=10, poll_s=1)
    assert results["agent-a"]["isp"] == "mobile" and results["agent-a"]["items"][0]["ping"]["rtts"] == [90, 95]
    # 结束后任务对象被删除，agent 不会再领到
    assert "Contents" not in s3.list_objects_v2(Bucket="xb-agent", Prefix="crossborder-agent/jobs/")


@mock_aws
def test_s3_broker_collect_timeout_returns_partial():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="xb-agent")
    t = [0.0]
    broker = S3AgentBroker(s3, "xb-agent", "p", clock=lambda: t[0], sleeper=lambda s: t.__setitem__(0, t[0] + s))
    job = broker.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)
    assert broker.collect(job["job_id"], min_agents=1, timeout_s=3, poll_s=1) == {}
    assert t[0] >= 3


# ---------- RemoteAgentBackend ----------

class FakeBroker:
    def __init__(self, results):
        self.results, self.published = results, []
    def publish(self, ips, ping_count, tcp_ports, tcp_count, ttl_s):
        self.published.append((ips, ping_count, tcp_ports, tcp_count))
        return {"job_id": "j1", "ips": ips}
    def collect(self, job_id, min_agents, timeout_s, poll_s=1.0):
        return self.results


def test_remote_backend_merges_agents_and_applies_isp_override():
    results = {"a-bj": {"isp": "telecom", "items": _items("1.1.1.1", [80, 82]) + _items("2.2.2.2", [120, 130])},
               "a-sh": {"isp": "reported", "items": _items("1.1.1.1", [70, 71])}}
    cfg = {**CFG, "instances": {"a-sh": "unicom"}}  # instances 在远程传输下是 agent_id -> isp 的覆盖表
    b = RemoteAgentBackend(FakeBroker(results), cfg)
    out = b.probe([Candidate("i-a", "1.1.1.1"), Candidate("i-b", "2.2.2.2")])
    assert b.name == "agent"
    p1 = out["1.1.1.1"]
    assert p1.ok and {p.isp for p in p1.probes} == {"telecom", "unicom"}
    ping_bj = next(p for p in p1.probes if p.isp == "telecom" and p.method == "ping")
    assert ping_bj.sent == 2 and ping_bj.received == 2 and ping_bj.median_rtt_ms == 81.0 and ping_bj.p95_rtt_ms == 82
    assert out["2.2.2.2"].ok and {p.isp for p in out["2.2.2.2"].probes} == {"telecom"}


def test_remote_backend_no_results_is_error_and_publishes_once():
    broker = FakeBroker({})
    out = RemoteAgentBackend(broker, CFG).probe([Candidate("i-a", "1.1.1.1"), Candidate("i-b", "2.2.2.2")])
    assert len(broker.published) == 1 and broker.published[0][0] == ["1.1.1.1", "2.2.2.2"]
    assert not out["1.1.1.1"].ok and "no agent result" in out["1.1.1.1"].error


def test_get_or_start_listener_does_not_deadlock_and_reuses_port():
    """回归：曾在持有模块锁时再次调用 shared_store() 造成死锁，监听器永远起不来。"""
    from crossborder_selector.probes import agent_transport as at
    result = {}

    def run():
        result["srv"] = at.get_or_start_listener("127.0.0.1:0", token="t")
    th = threading.Thread(target=run, daemon=True)
    th.start(); th.join(timeout=5)
    assert not th.is_alive(), "get_or_start_listener 卡住（死锁）"
    srv = result["srv"]
    try:
        assert srv.port > 0
        assert at.get_or_start_listener("127.0.0.1:0", token="t") is srv  # 同一监听地址复用
        status, body = _get(f"http://127.0.0.1:{srv.port}/api/agent/registry", token="t")
        assert status == 200
    finally:
        srv.stop()
        at._LISTENERS.pop("127.0.0.1:0", None)


def test_registry_records_remote_ip_from_claims_and_submits():
    store = AgentJobStore(clock=lambda: 1.0)
    store.claim("a1", isp="telecom", remote_addr="203.0.113.7")
    assert store.registry()["a1"]["ip"] == "203.0.113.7"
    job = store.publish(["1.1.1.1"], 4, [443], 3, ttl_s=60)
    store.submit(job["job_id"], "a2", "unicom", _items("1.1.1.1", [1]), remote_addr="198.51.100.9")
    assert store.registry()["a2"]["ip"] == "198.51.100.9"
    assert store.registry()["a1"]["ip"] == "203.0.113.7"  # 未带地址的后续调用不覆盖
    store.claim("a1", isp="telecom")
    assert store.registry()["a1"]["ip"] == "203.0.113.7"


def test_http_transport_records_client_ip_in_registry(http_server):
    store, base = http_server
    _get(f"{base}/api/agent/jobs?agent_id=lap&isp=telecom", token="secret")
    assert store.registry()["lap"]["ip"] == "127.0.0.1"
