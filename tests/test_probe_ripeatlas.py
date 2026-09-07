from crossborder_selector.models import Candidate
from crossborder_selector.probes.ripeatlas import RipeAtlasBackend, isp_for_asn, API_BASE

CFG = {"enabled": True, "api_key": "KEY", "probe_count": 3, "packets": 4, "timeout_s": 120}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class FakeHttp:
    def __init__(self):
        self.calls, self.polls = [], 0
    def __call__(self, method, url, body=None, params=None):
        self.calls.append((method, url, body, params))
        if method == "POST":
            return {"measurements": [77]}
        if url.endswith("/measurements/77/results/"):
            self.polls += 1
            if self.polls == 1:
                return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 180.0}]
            return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 180.0},
                    {"prb_id": 2, "sent": 4, "rcvd": 2, "avg": 240.0},
                    {"prb_id": 3, "sent": 4, "rcvd": 0, "avg": -1}]
        if url.endswith("/probes/1/"):
            return {"id": 1, "asn_v4": 4134}
        if url.endswith("/probes/2/"):
            return {"id": 2, "asn_v4": 9808}
        if url.endswith("/probes/3/"):
            return {"id": 3, "asn_v4": 12345}
        raise AssertionError(url)


def test_asn_mapping():
    assert isp_for_asn(4134) == "telecom" and isp_for_asn(4837) == "unicom"
    assert isp_for_asn(56046) == "mobile" and isp_for_asn(99999) == "other"


def test_create_poll_and_map():
    clk, http = FakeClock(), FakeHttp()
    b = RipeAtlasBackend(CFG, http=http, sleeper=clk.sleep, clock=clk)
    pr = b.probe([Candidate("i", "18.162.1.1")])["18.162.1.1"]
    m, url, body, params = http.calls[0]
    assert m == "POST" and url == f"{API_BASE}/measurements/" and params == {"key": "KEY"}
    d = body["definitions"][0]
    assert d["target"] == "18.162.1.1" and d["type"] == "ping" and d["af"] == 4 and d["packets"] == 4
    assert body["probes"] == [{"type": "country", "value": "CN", "requested": 3}] and body["is_oneoff"] is True
    assert pr.ok and [p.isp for p in pr.probes] == ["telecom", "mobile", "other"]
    assert pr.probes[2].median_rtt_ms is None and pr.probes[1].received == 2


def test_partial_results_on_timeout_still_ok():
    class Slow(FakeHttp):
        def __call__(self, method, url, body=None, params=None):
            if url.endswith("/results/"):
                self.calls.append((method, url, body, params))
                return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 100.0}]
            return super().__call__(method, url, body, params)
    clk = FakeClock()
    pr = RipeAtlasBackend({**CFG, "timeout_s": 25}, http=Slow(), sleeper=clk.sleep, clock=clk).probe([Candidate("i", "1.1.1.1")])["1.1.1.1"]
    assert pr.ok and len(pr.probes) == 1


def test_no_results_is_error():
    class Empty(FakeHttp):
        def __call__(self, method, url, body=None, params=None):
            return {"measurements": [77]} if method == "POST" else []
    clk = FakeClock()
    pr = RipeAtlasBackend({**CFG, "timeout_s": 20}, http=Empty(), sleeper=clk.sleep, clock=clk).probe([Candidate("i", "1.1.1.1")])["1.1.1.1"]
    assert not pr.ok and "no results" in pr.error
