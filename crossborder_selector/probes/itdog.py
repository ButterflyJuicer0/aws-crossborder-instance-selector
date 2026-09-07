"""itdog.cn 批量 ping 的非官方封装。协议来自社区逆向，随时可能失效；任何失败只记 error。"""
import base64
import hashlib
import json
import re
import time

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

BATCH_PING_URL = "https://www.itdog.cn/batch_ping/"
TASK_TOKEN_SECRET = "token_20230313000136kwyktxb0tgspm00yo5"
GUARD_XOR_SUFFIX = "PTNo2n3Ev5"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_RE_WSS = re.compile(r"var wss_url='(.*?)';")
_RE_TASK = re.compile(r"var task_id='(.*?)';")


def generate_guardret(guard: str) -> str:
    key = guard[:8] + GUARD_XOR_SUFFIX
    num = int(guard[12:]) if len(guard) > 12 else 0
    value = str(num * 2 + 18 - 2)
    enc = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(value))
    return base64.b64encode(enc.encode()).decode()


def task_token(task_id: str) -> str:
    return hashlib.md5((task_id + TASK_TOKEN_SECRET).encode()).hexdigest()[8:-8]


def parse_page(html: str):
    m1, m2 = _RE_WSS.search(html or ""), _RE_TASK.search(html or "")
    if not m1 or not m2:
        raise ValueError("itdog page missing wss_url/task_id (protocol changed?)")
    return m1.group(1), m2.group(1)


def _default_ws_connect(url):
    from websockets.sync.client import connect  # 延迟导入：只有启用 itdog 才需要该依赖
    return connect(url, open_timeout=10, close_timeout=5)


class ItdogBackend(ProbeBackend):
    name = "itdog"

    def __init__(self, backend_cfg, session=None, ws_connect=None, clock=time.time):
        self.cfg, self._now = backend_cfg, clock
        self.nodes = {str(k): v for k, v in backend_cfg["nodes"].items()}
        if session is None:
            import requests
            session = requests.Session()
        self.session, self.ws_connect = session, ws_connect or _default_ws_connect

    def _start_task(self, ips):
        headers = {"User-Agent": UA, "Referer": BATCH_PING_URL,
                   "Content-Type": "application/x-www-form-urlencoded"}
        data = {"host": "\r\n".join(ips), "node_id": ",".join(self.nodes), "cidr_filter": "true", "gateway": "last"}
        resp = self.session.post(BATCH_PING_URL, headers=headers, data=data)
        if "guardret" not in self.session.cookies and "guard" in self.session.cookies:
            self.session.cookies["guardret"] = generate_guardret(self.session.cookies["guard"])
            resp = self.session.post(BATCH_PING_URL, headers=headers, data=data)
        return parse_page(resp.text)

    def _collect(self, wss_url, task_id, ips) -> dict:
        samples = {ip: [] for ip in ips}
        deadline = self._now() + self.cfg["timeout_s"]
        with self.ws_connect(wss_url) as ws:
            ws.send(json.dumps({"task_id": task_id, "task_token": task_token(task_id)}))
            while self._now() < deadline:
                try:
                    msg = json.loads(ws.recv())
                except (TimeoutError, OSError, ValueError):
                    break
                if msg.get("type") == "finished":
                    break
                ip, node = msg.get("ip"), str(msg.get("node_id", ""))
                if ip not in samples or node not in self.nodes:
                    continue
                raw = str(msg.get("result", "")).strip()
                try:
                    rtt, ok = float(raw), 1
                except ValueError:
                    rtt, ok = None, 0
                samples[ip].append(IspProbe(isp=self.nodes[node], sent=1, received=ok, median_rtt_ms=rtt,
                                            target=f"node:{node}", method="ping"))
        return samples

    def probe(self, candidates) -> dict:
        ips = [c.public_ip for c in candidates]
        try:
            wss_url, task_id = self._start_task(ips)
            samples = self._collect(wss_url, task_id, ips)
        except Exception as e:
            return {ip: ProbeResult(self.name, [], f"itdog failed: {type(e).__name__}: {e}") for ip in ips}
        return {ip: (ProbeResult(self.name, s) if s else ProbeResult(self.name, [], "itdog returned no samples"))
                for ip, s in samples.items()}
