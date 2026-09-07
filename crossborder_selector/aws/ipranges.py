"""AWS 公布的 ip-ranges.json：下载、缓存、IP → prefix。"""
import ipaddress
import json
import os
import urllib.request

IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode()


def load_ip_ranges(fetcher=None, cache_path=None) -> list:
    """返回 prefixes 列表；有缓存文件先读缓存；任何失败返回 []。"""
    fetcher = fetcher or _http_get
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f).get("prefixes", [])
        except (OSError, ValueError):
            pass
    try:
        text = fetcher(IP_RANGES_URL)
        data = json.loads(text)
    except Exception:
        return []
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w") as f:
                f.write(text)
        except OSError:
            pass
    return data.get("prefixes", [])


def prefix_for(ip: str, prefixes: list, region: str) -> str:
    """优先 EC2 服务、同 region；其次最长前缀。找不到返回空串。"""
    addr = ipaddress.ip_address(ip)
    best, best_key = "", None
    for p in prefixes:
        try:
            net = ipaddress.ip_network(p["ip_prefix"])
        except (KeyError, ValueError):
            continue
        if addr not in net:
            continue
        key = (p.get("service") == "EC2", p.get("region") == region, net.prefixlen)
        if best_key is None or key > best_key:
            best, best_key = p["ip_prefix"], key
    return best


class PrefixLookup:
    def __init__(self, prefixes: list, region: str):
        self.prefixes, self.region = prefixes, region

    def __call__(self, ip: str) -> str:
        return prefix_for(ip, self.prefixes, self.region)
