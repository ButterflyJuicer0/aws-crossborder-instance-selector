import ipaddress

import dns.resolver
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource

# 名单命中的合法响应段；Spamhaus/DNSBL 约定用 127.0.0.0/24 内的地址编码命中类型。
_LISTED_NET = ipaddress.ip_network("127.0.0.0/24")
# 127.255.255.0/24 是错误码段（如 127.255.255.254=经开放递归查询，127.255.255.255=被限速），不是命中。
_ERROR_NET = ipaddress.ip_network("127.255.255.0/24")


def reverse_ip(address: str) -> str:
    return ".".join(reversed(address.split(".")))


class DnsblSource(ReputationSource):
    name = "dnsbl"

    def __init__(self, zones, weight: float = 50.0, resolver=None):
        self.zones = zones
        self.weight = weight
        self._resolver = resolver or dns.resolver.Resolver()

    def check(self, address: str) -> SourceResult:
        rev, hits, errors = reverse_ip(address), [], []
        for zone in self.zones:
            try:
                answers = self._resolver.resolve(f"{rev}.{zone}", "A")
            except dns.resolver.NXDOMAIN:
                continue  # DNSBL 通过 NXDOMAIN 表示未命中。
            except Exception as exc:
                errors.append(f"{zone}: {type(exc).__name__}: {exc}")
                continue
            listed_here, err_code = False, None
            for ans in answers:
                try:
                    ip = ipaddress.ip_address(str(ans))
                except ValueError:
                    errors.append(f"{zone}: unexpected response {ans}")
                    continue
                if ip in _ERROR_NET:
                    err_code = str(ans)
                elif ip in _LISTED_NET:
                    listed_here = True
                else:
                    errors.append(f"{zone}: unexpected response {ans}")
            if listed_here:
                hits.append(zone)
            elif err_code is not None:
                errors.append(f"{zone}={err_code}")
        if hits:
            return SourceResult(self.name, True, ",".join(hits), ";".join(errors))
        if errors:
            return SourceResult(self.name, False, "error:" + ";".join(errors), ";".join(errors))
        return SourceResult(self.name, False, "")
