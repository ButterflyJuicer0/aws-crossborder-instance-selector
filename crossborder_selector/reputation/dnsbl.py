import dns.resolver
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource


def reverse_ip(address: str) -> str:
    return ".".join(reversed(address.split(".")))


class DnsblSource(ReputationSource):
    name = "dnsbl"

    def __init__(self, zones, weight: float = 50.0, resolver=None):
        self.zones = zones
        self.weight = weight
        self._resolver = resolver or dns.resolver.Resolver()

    def check(self, address: str) -> SourceResult:
        rev, hits = reverse_ip(address), []
        for zone in self.zones:
            try:
                if self._resolver.resolve(f"{rev}.{zone}", "A"):
                    hits.append(zone)
            except Exception:
                continue  # 未命中或解析失败均视为该 zone 不在名单
        return SourceResult(self.name, bool(hits), ",".join(hits))
