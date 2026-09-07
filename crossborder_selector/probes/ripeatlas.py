"""RIPE Atlas：从大陆在线探针 one-off ping 候选 IP。需要 API key（免费申请 credits）。"""
import time

import requests

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

API_BASE = "https://atlas.ripe.net/api/v2"
ASN_ISP = {
    "telecom": {4134, 4812, 23724, 4809, 58466, 134762},
    "unicom": {4837, 4808, 17816, 17622, 4847, 9929},
    "mobile": {9808, 56040, 56041, 56042, 56044, 56045, 56046, 56047, 56048, 24444, 24445, 24547, 9394},
}


def isp_for_asn(asn) -> str:
    for isp, asns in ASN_ISP.items():
        if asn in asns:
            return isp
    return "other"


def _default_http(method, url, body=None, params=None):
    r = requests.request(method, url, json=body, params=params, timeout=20,
                         headers={"User-Agent": "crossborder-selector"})
    r.raise_for_status()
    return r.json()


class RipeAtlasBackend(ProbeBackend):
    name = "ripeatlas"

    def __init__(self, backend_cfg, http=None, sleeper=time.sleep, clock=time.time, poll_s=10):
        self.cfg, self.http = backend_cfg, http or _default_http
        self._sleep, self._now, self.poll_s = sleeper, clock, poll_s
        self._asn_cache = {}

    def _asn(self, prb_id) -> int:
        if prb_id not in self._asn_cache:
            self._asn_cache[prb_id] = int(self.http("GET", f"{API_BASE}/probes/{prb_id}/").get("asn_v4") or 0)
        return self._asn_cache[prb_id]

    def _measure(self, ip) -> ProbeResult:
        body = {"definitions": [{"target": ip, "af": 4, "type": "ping", "packets": self.cfg["packets"],
                                 "description": f"crossborder-selector {ip}", "is_oneoff": True}],
                "probes": [{"type": "country", "value": "CN", "requested": self.cfg["probe_count"]}],
                "is_oneoff": True}
        mid = self.http("POST", f"{API_BASE}/measurements/", body, {"key": self.cfg["api_key"]})["measurements"][0]
        deadline, results = self._now() + self.cfg["timeout_s"], []
        while True:
            self._sleep(self.poll_s)
            results = self.http("GET", f"{API_BASE}/measurements/{mid}/results/") or []
            if len(results) >= self.cfg["probe_count"] or self._now() >= deadline:
                break
        if not results:
            return ProbeResult(self.name, [], f"ripeatlas no results for measurement {mid}")
        probes = []
        for r in results:
            avg = r.get("avg")
            probes.append(IspProbe(isp=isp_for_asn(self._asn(r["prb_id"])), sent=int(r.get("sent") or 0),
                                   received=int(r.get("rcvd") or 0),
                                   median_rtt_ms=(None if avg is None or avg < 0 else float(avg)),
                                   target=ip, method="ping"))
        return ProbeResult(self.name, probes)

    def probe(self, candidates) -> dict:
        out = {}
        for c in candidates:
            try:
                out[c.public_ip] = self._measure(c.public_ip)
            except Exception as e:
                out[c.public_ip] = ProbeResult(self.name, [], f"{type(e).__name__}: {e}")
        return out
