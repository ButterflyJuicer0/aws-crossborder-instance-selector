import json
from crossborder_selector.aws.ipranges import load_ip_ranges, prefix_for, PrefixLookup

RANGES = {"prefixes": [
    {"ip_prefix": "18.162.0.0/16", "region": "ap-east-1", "service": "EC2"},
    {"ip_prefix": "18.160.0.0/13", "region": "ap-east-1", "service": "AMAZON"},
    {"ip_prefix": "43.198.0.0/15", "region": "ap-east-1", "service": "AMAZON"},
    {"ip_prefix": "3.0.0.0/8", "region": "GLOBAL", "service": "AMAZON"},
]}


def test_load_uses_fetcher_and_returns_prefixes():
    got = load_ip_ranges(fetcher=lambda url: json.dumps(RANGES))
    assert len(got) == 4


def test_load_failure_returns_empty():
    def boom(url):
        raise OSError("offline")
    assert load_ip_ranges(fetcher=boom) == []


def test_prefix_prefers_ec2_then_most_specific():
    p = RANGES["prefixes"]
    assert prefix_for("18.162.1.1", p, "ap-east-1") == "18.162.0.0/16"
    assert prefix_for("43.198.9.9", p, "ap-east-1") == "43.198.0.0/15"
    assert prefix_for("3.4.5.6", p, "ap-east-1") == "3.0.0.0/8"
    assert prefix_for("8.8.8.8", p, "ap-east-1") == ""


def test_cache_written_and_reused(tmp_path):
    calls = {"n": 0}
    def fetcher(url):
        calls["n"] += 1
        return json.dumps(RANGES)
    cache = tmp_path / "ipr.json"
    load_ip_ranges(fetcher=fetcher, cache_path=str(cache))
    load_ip_ranges(fetcher=fetcher, cache_path=str(cache))
    assert calls["n"] == 1 and cache.exists()


def test_lookup_callable():
    lk = PrefixLookup(RANGES["prefixes"], "ap-east-1")
    assert lk("18.162.0.5") == "18.162.0.0/16"
