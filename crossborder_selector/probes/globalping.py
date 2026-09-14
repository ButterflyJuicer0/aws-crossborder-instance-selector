"""Globalping 公共探针：从 HK/TW 视角 ping 候选 IP。无需 API key。"""
import time

import requests

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

API_BASE = "https://api.globalping.io/v1"


def _default_http(method, url, body=None, headers=None):
    base = {"Content-Type": "application/json", "User-Agent": "crossborder-selector"}
    base.update(headers or {})
    r = requests.request(method, url, json=body, timeout=20, headers=base)
    r.raise_for_status()
    return r.json()


class GlobalpingBackend(ProbeBackend):
    name = "globalping"

    def __init__(self, backend_cfg, http=None, sleeper=time.sleep, clock=time.time, poll_s=2):
        self.cfg, self.http = backend_cfg, http or _default_http
        self._sleep, self._now, self.poll_s = sleeper, clock, poll_s
        token = (self.cfg.get("api_token") or "").strip()
        # token 用于认证；额度取决于服务当前规则及账户状态。
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _measure(self, ip) -> ProbeResult:
        body = {"type": "ping", "target": ip,
                "locations": [{"country": c, "limit": self.cfg["limit_per_location"]} for c in self.cfg["locations"]],
                "measurementOptions": {"packets": self.cfg["packets"]}}
        mid = self.http("POST", f"{API_BASE}/measurements", body, headers=self.headers)["id"]
        deadline = self._now() + self.cfg["timeout_s"]
        while True:
            self._sleep(self.poll_s)
            data = self.http("GET", f"{API_BASE}/measurements/{mid}", headers=self.headers)
            if data.get("status") == "finished":
                break
            if self._now() >= deadline:
                return ProbeResult(self.name, [], f"globalping timeout for measurement {mid}")
        probes = []
        for r in data.get("results", []):
            result = r.get("result") or {}
            if result.get("status") != "finished":
                continue  # 离线/失败探针没有 stats，跳过而非记为 100% 丢包
            st = result.get("stats") or {}
            probes.append(IspProbe(isp=r["probe"]["country"], sent=int(st.get("total") or 0),
                                   received=int(st.get("rcv") or 0),
                                   median_rtt_ms=(None if st.get("avg") is None else float(st["avg"])),
                                   target=ip, method="ping"))
        return ProbeResult(self.name, probes)

    def probe(self, candidates) -> dict:
        out = {}
        for c in candidates:  # 串行提交，遵守公共 API 速率限制
            try:
                out[c.public_ip] = self._measure(c.public_ip)
            except Exception as e:
                out[c.public_ip] = ProbeResult(self.name, [], f"{type(e).__name__}: {e}")
        return out
