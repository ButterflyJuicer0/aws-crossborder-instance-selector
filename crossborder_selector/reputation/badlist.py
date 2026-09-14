import requests
import ipaddress
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource


def _http_get(url: str) -> str:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


class BadListSource(ReputationSource):
    name = "badlist"

    def __init__(self, url: str, weight: float = 80.0, fetcher=None):
        self.url, self.weight = url, weight
        self._fetcher = fetcher or _http_get
        self._ips = None
        self._networks = []
        self._load_error = None

    def _load(self):
        if self._load_error:
            raise ValueError(self._load_error)
        if self._ips is None:
            try:
                text = self._fetcher(self.url)
                ips, networks = set(), []
                for number, line in enumerate(text.splitlines(), 1):
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    token = line.split()[0]
                    try:
                        if "/" in token:
                            networks.append(ipaddress.ip_network(token, strict=False))
                        else:
                            ips.add(str(ipaddress.ip_address(token)))
                    except ValueError as exc:
                        raise ValueError(f"IP 名单第 {number} 行不是有效 IP/CIDR；请填写原始名单文件 URL") from exc
                if not ips and not networks:
                    raise ValueError("IP 名单为空，无法完成检查")
                self._ips, self._networks = ips, networks
            except Exception as exc:
                self._load_error = f"{self.url}: {exc}"
                raise ValueError(self._load_error) from exc
        return self._ips

    def check(self, address: str) -> SourceResult:
        ip = ipaddress.ip_address(address)
        match = str(ip) if str(ip) in self._load() else next(
            (str(net) for net in self._networks if net.version == ip.version and ip in net), "")
        detail = f"{self.url} · {len(self._ips) + len(self._networks)} 条"
        if match:
            detail += f" · 命中 {match}"
        return SourceResult(self.name, bool(match), detail)
