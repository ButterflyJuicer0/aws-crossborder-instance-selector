from crossborder_selector.models import Candidate
from crossborder_selector.probes.globalping import GlobalpingBackend, API_BASE

CFG = {"enabled": True, "locations": ["HK", "TW"], "limit_per_location": 2, "packets": 4, "timeout_s": 60}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def _probe(country):
    # 真实 API 的 probe 是扁平 ProbeLocation，country 在顶层
    return {"continent": "AS", "region": "Eastern Asia", "country": country, "state": None,
            "city": "Hong Kong" if country == "HK" else "Taipei", "asn": 4760, "network": "example net"}


def _finished(ip):
    return {"id": "m1", "status": "finished", "results": [
        {"probe": _probe("HK"), "result": {"status": "finished", "stats": {"total": 4, "rcv": 4, "avg": 12.5}}},
        {"probe": _probe("HK"), "result": {"status": "finished", "stats": {"total": 4, "rcv": 3, "avg": 15.0}}},
        {"probe": _probe("TW"), "result": {"status": "finished", "stats": {"total": 4, "rcv": 0, "avg": None}}},
        {"probe": _probe("TW"), "result": {"status": "offline"}},  # 离线探针无 stats，必须被跳过
    ]}


class FakeHttp:
    def __init__(self, polls_before_finish=1):
        self.calls, self.n = [], polls_before_finish
    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body, headers or {}))
        if method == "POST":
            return {"id": "m1", "probesCount": 3}
        self.n -= 1
        return {"id": "m1", "status": "in-progress", "results": []} if self.n >= 0 else _finished("x")


def test_request_body_and_parsing():
    clk, http = FakeClock(), FakeHttp()
    b = GlobalpingBackend(CFG, http=http, sleeper=clk.sleep, clock=clk)
    out = b.probe([Candidate("i-1", "18.162.1.1")])
    m, url, body, _hdrs = http.calls[0]
    assert m == "POST" and url == f"{API_BASE}/measurements"
    assert body["type"] == "ping" and body["target"] == "18.162.1.1"
    assert body["locations"] == [{"country": "HK", "limit": 2}, {"country": "TW", "limit": 2}]
    assert body["measurementOptions"] == {"packets": 4}
    pr = out["18.162.1.1"]
    # 离线的第 4 个 TW 结果被跳过，只保留 3 个 finished 探针
    assert pr.ok and len(pr.probes) == 3 and [p.isp for p in pr.probes] == ["HK", "HK", "TW"]
    assert pr.probes[0].median_rtt_ms == 12.5 and pr.probes[2].received == 0 and pr.probes[2].median_rtt_ms is None


def test_timeout_yields_error():
    clk = FakeClock()
    b = GlobalpingBackend({**CFG, "timeout_s": 5}, http=FakeHttp(polls_before_finish=99), sleeper=clk.sleep, clock=clk, poll_s=2)
    pr = b.probe([Candidate("i-1", "1.1.1.1")])["1.1.1.1"]
    assert not pr.ok and "timeout" in pr.error


def test_http_error_isolated_per_candidate():
    def http(m, u, b=None, headers=None):
        if b and b["target"] == "2.2.2.2":
            raise RuntimeError("429 too many")
        return {"id": "m1"} if m == "POST" else _finished("x")
    clk = FakeClock()
    out = GlobalpingBackend(CFG, http=http, sleeper=clk.sleep, clock=clk).probe([Candidate("a", "1.1.1.1"), Candidate("b", "2.2.2.2")])
    assert out["1.1.1.1"].ok and "429" in out["2.2.2.2"].error


def test_no_auth_header_without_token():
    clk, http = FakeClock(), FakeHttp()
    GlobalpingBackend(CFG, http=http, sleeper=clk.sleep, clock=clk).probe([Candidate("i-1", "1.1.1.1")])
    assert all("Authorization" not in hdrs for _, _, _, hdrs in http.calls)


def test_sends_bearer_token_when_configured():
    clk, http = FakeClock(), FakeHttp()
    GlobalpingBackend({**CFG, "api_token": "secret"}, http=http, sleeper=clk.sleep, clock=clk).probe(
        [Candidate("i-1", "1.1.1.1")])
    assert http.calls and all(hdrs.get("Authorization") == "Bearer secret" for _, _, _, hdrs in http.calls)
