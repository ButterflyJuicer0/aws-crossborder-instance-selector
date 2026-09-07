from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource, score_reputation
from crossborder_selector.reputation.dnsbl import DnsblSource, reverse_ip
from crossborder_selector.reputation.badlist import BadListSource
from crossborder_selector.reputation.abuseipdb import AbuseIpdbSource, build_sources


class FakeSource(ReputationSource):
    def __init__(self, name, weight, listed):
        self.name = name; self.weight = weight; self._listed = listed
    def check(self, address):
        return SourceResult(self.name, self._listed, "x" if self._listed else "")


class BoomSource(ReputationSource):
    name = "boom"; weight = 50.0
    def check(self, address):
        raise RuntimeError("dns timeout")


def test_clean_ip_scores_100():
    rr = score_reputation("1.2.3.4", [FakeSource("a", 40, False), FakeSource("b", 60, False)])
    assert rr.score == 100.0 and rr.any_listed is False


def test_listed_ip_loses_weight_and_floors_at_zero():
    assert score_reputation("1.2.3.4", [FakeSource("a", 40, True), FakeSource("b", 60, False)]).score == 60.0
    assert score_reputation("1.2.3.4", [FakeSource("a", 70, True), FakeSource("b", 60, True)]).score == 0.0


def test_source_error_does_not_deduct():
    rr = score_reputation("1.2.3.4", [BoomSource()])
    assert rr.score == 100.0 and rr.results[0].detail.startswith("error:")


def test_dnsbl():
    assert reverse_ip("1.2.3.4") == "4.3.2.1"
    class R:
        def resolve(self, q, t):
            if q == "4.3.2.1.zen.example.org":
                return ["127.0.0.2"]
            raise Exception("NXDOMAIN")
    s = DnsblSource(zones=["zen.example.org", "bl.example.org"], resolver=R())
    assert s.check("1.2.3.4").listed is True
    assert s.check("5.6.7.8").listed is False


def test_badlist_hit_miss_and_single_fetch():
    calls = {"n": 0}
    def fetcher(u):
        calls["n"] += 1
        return "# c\n45.78.235.240\n\n"
    s = BadListSource(url="http://x", fetcher=fetcher)
    assert s.check("45.78.235.240").listed is True
    assert s.check("1.1.1.1").listed is False
    assert calls["n"] == 1


def test_abuseipdb_threshold():
    hi = AbuseIpdbSource(api_key="k", caller=lambda k, a: {"abuseConfidenceScore": 90})
    lo = AbuseIpdbSource(api_key="k", caller=lambda k, a: {"abuseConfidenceScore": 0})
    assert hi.check("1.2.3.4").listed is True and lo.check("1.2.3.4").listed is False


def test_build_sources_key_toggle():
    base = {"dnsbl_zones": ["z"], "badlist_url": "http://x", "abuseipdb_api_key": ""}
    assert "abuseipdb" not in {s.name for s in build_sources(base)}
    assert "abuseipdb" in {s.name for s in build_sources({**base, "abuseipdb_api_key": "k"})}
