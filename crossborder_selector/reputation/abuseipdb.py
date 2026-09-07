import requests
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource
from crossborder_selector.reputation.dnsbl import DnsblSource
from crossborder_selector.reputation.badlist import BadListSource


def _default_caller(api_key: str, address: str) -> dict:
    resp = requests.get("https://api.abuseipdb.com/api/v2/check",
                        headers={"Key": api_key, "Accept": "application/json"},
                        params={"ipAddress": address, "maxAgeInDays": 90}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", {})


class AbuseIpdbSource(ReputationSource):
    name = "abuseipdb"

    def __init__(self, api_key: str, threshold: int = 25, weight: float = 70.0, caller=None):
        self.api_key, self.threshold, self.weight = api_key, threshold, weight
        self._caller = caller or _default_caller

    def check(self, address: str) -> SourceResult:
        score = int(self._caller(self.api_key, address).get("abuseConfidenceScore", 0))
        return SourceResult(self.name, score >= self.threshold, f"confidence={score}")


def build_sources(config: dict):
    sources = [DnsblSource(zones=config["dnsbl_zones"]), BadListSource(url=config["badlist_url"])]
    key = (config.get("abuseipdb_api_key") or "").strip()
    if key:
        sources.append(AbuseIpdbSource(api_key=key))
    return sources
