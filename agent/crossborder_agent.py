#!/usr/bin/env python3
"""crossborder-agent：在任意机器上运行的探测 agent（只依赖标准库；S3 模式需要 boto3）。

方向永远是 agent → 候选 IP（出向），本机不需要开放任何入站端口。

用法：
  # 一次性探测并打印结果（手工/调试）
  python3 crossborder_agent.py once --targets 1.1.1.1,2.2.2.2 --ports 443,22

  # HTTP 传输：轮询选择器（CLI 监听器或 Web 服务同一端口），领任务 → 探测 → 回传
  python3 crossborder_agent.py serve --server http://<selector-host>:8766 --token <token> \
      --agent-id bj-telecom-01 --isp telecom

  # S3 传输：任务和结果都在 bucket 里；只需要出向 HTTPS 与限定前缀的 AWS 凭证
  python3 crossborder_agent.py serve --s3 s3://<bucket>/crossborder-agent --agent-id sh-unicom-01 --isp unicom \
      [--profile <aws-profile>] [--region <s3-region>]

  --once 只跑一个轮询周期后退出（用于验证部署）。--interval 控制轮询间隔（秒）。

输出格式与 SSM 版 agent 一致：以 CROSSBORDER_AGENT_JSON: 开头的一行 JSON：
  [{"ip": ..., "ping": {"sent": N, "rtts": [...]}, "tcp": [{"port": P, "attempts": M, "connect_ms": [...]}]}]
"""
from __future__ import annotations

import argparse
import json
import platform as _platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

MARKER = "CROSSBORDER_AGENT_JSON:"
_TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms")
PUBLIC_IP_URL = "https://checkip.amazonaws.com"


def _default_fetch(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def detect_public_ip(fetcher=None):
    """本机公网出口 IP（用于选择器自动放行拨测来源）。失败返回 None；结果按 fetcher 缓存。"""
    key = fetcher or _default_fetch
    if key in detect_public_ip._cache:
        return detect_public_ip._cache[key]
    value = None
    try:
        text = (fetcher or _default_fetch)(PUBLIC_IP_URL, 5).decode().strip()
        import ipaddress
        value = str(ipaddress.ip_address(text))
    except Exception:  # noqa: BLE001 - 离线或返回非 IP 都视为未知
        value = None
    detect_public_ip._cache[key] = value
    return value


detect_public_ip._cache = {}
detect_public_ip.cache_clear = detect_public_ip._cache.clear


# ---------- 探测 ----------

def ping_command(ip: str, count: int, platform: str | None = None) -> list:
    plat = (platform or sys.platform).lower()
    if plat.startswith("darwin") or plat.startswith("freebsd"):
        # macOS/BSD：-i 最小 0.1 需 root，用 0.2；-W 语义不同，不传
        return ["ping", "-c", str(count), "-i", "0.2", ip]
    if plat.startswith("win"):
        return ["ping", "-n", str(count), "-w", "2000", ip]
    return ["ping", "-c", str(count), "-i", "0.2", "-W", "2", ip]


def parse_ping_output(text: str) -> list:
    """从 ping 输出提取每个回包的 RTT（ms），兼容 Linux iputils、macOS/BSD、Windows。"""
    return [float(m.group(1)) for m in _TIME_RE.finditer(text)]


def run_ping(ip: str, count: int) -> list:
    try:
        r = subprocess.run(ping_command(ip, count), capture_output=True, text=True, timeout=count * 3 + 10)
        return parse_ping_output(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return []


def tcp_connect_times(ip: str, port: int, attempts: int, timeout_s: float = 3.0) -> list:
    """逐次 TCP 三次握手耗时（ms，亚毫秒精度）；失败的尝试不计入，丢失率 = 1 - len/attempts。"""
    out = []
    for _ in range(attempts):
        s = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        t0 = time.perf_counter()
        try:
            s.connect((ip, int(port)))
            out.append(round((time.perf_counter() - t0) * 1000.0, 3))
        except OSError:
            pass
        finally:
            s.close()
    return out


def probe_targets(ips: list, ping_count: int, tcp_ports: list, tcp_count: int) -> list:
    items = []
    for ip in ips:
        rtts = run_ping(ip, ping_count)
        tcp = [{"port": int(p), "attempts": int(tcp_count), "connect_ms": tcp_connect_times(ip, int(p), int(tcp_count))}
               for p in tcp_ports]
        items.append({"ip": ip, "ping": {"sent": int(ping_count), "rtts": rtts}, "tcp": tcp})
    return items


# ---------- 传输 ----------

class HttpTransport:
    def __init__(self, server: str, token: str, agent_id: str, isp: str, timeout_s: float = 20.0, public_ip: str = ""):
        self.base, self.token, self.agent_id, self.isp, self.timeout = server.rstrip("/"), token, agent_id, isp, timeout_s
        self.public_ip = public_ip or ""

    def _req(self, method, path, body=None):
        headers = {"User-Agent": "crossborder-agent"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)

    def claim(self):
        q = f"/api/agent/jobs?agent_id={self.agent_id}&isp={self.isp}"
        if self.public_ip:
            q += f"&public_ip={self.public_ip}"
        status, job = self._req("GET", q)
        return job if status == 200 else None

    def submit(self, job_id: str, items: list):
        self._req("POST", "/api/agent/results", {"job_id": job_id, "agent_id": self.agent_id, "isp": self.isp,
                                                  "public_ip": self.public_ip, "items": items})


def make_s3_client(profile: str | None, region: str | None):
    try:
        import boto3  # 仅 S3 模式需要：pip install boto3
    except ImportError:
        sys.exit("S3 模式需要 boto3：pip install boto3")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3", region_name=region) if region else session.client("s3")


class S3Transport:
    """任务：<prefix>/jobs/<job_id>.json；结果：<prefix>/results/<job_id>/<agent_id>.json。"""

    def __init__(self, s3_client, bucket: str, prefix: str, agent_id: str, isp: str, public_ip: str = ""):
        self.s3, self.bucket, self.prefix = s3_client, bucket, prefix.strip("/")
        self.agent_id, self.isp, self.public_ip = agent_id, isp, public_ip or ""
        self._done = set()

    def heartbeat(self):
        """registry/<agent_id>.json：让选择器知道有哪些 agent、各自的公网出口与运营商。"""
        body = {"agent_id": self.agent_id, "isp": self.isp, "public_ip": self.public_ip, "last_seen": time.time()}
        self.s3.put_object(Bucket=self.bucket, Key=self._key("registry", f"{self.agent_id}.json"),
                           Body=json.dumps(body).encode(), ContentType="application/json")

    def _key(self, *parts):
        return "/".join([self.prefix, *parts]) if self.prefix else "/".join(parts)

    def claim(self):
        try:
            self.heartbeat()
        except Exception:  # noqa: BLE001 - 心跳失败不影响领任务
            pass
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self._key("jobs") + "/")
        now = time.time()
        for obj in sorted(resp.get("Contents", []), key=lambda o: o["Key"]):
            job_id = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
            if job_id in self._done:
                continue
            try:
                job = json.loads(self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read())
            except Exception:
                continue
            if job.get("expires_at") and job["expires_at"] < now:
                continue
            # 已经提交过结果的任务不再重复跑
            try:
                self.s3.head_object(Bucket=self.bucket, Key=self._key("results", job_id, f"{self.agent_id}.json"))
                self._done.add(job_id)
                continue
            except Exception:
                pass
            return job
        return None

    def submit(self, job_id: str, items: list):
        body = {"agent_id": self.agent_id, "isp": self.isp, "public_ip": self.public_ip, "items": items,
                "submitted_at": time.time()}
        self.s3.put_object(Bucket=self.bucket, Key=self._key("results", job_id, f"{self.agent_id}.json"),
                           Body=json.dumps(body).encode(), ContentType="application/json")
        self._done.add(job_id)


def parse_s3_url(url: str):
    if not url.startswith("s3://"):
        raise ValueError("--s3 需要 s3://bucket/prefix 形式")
    bucket, _, prefix = url[5:].partition("/")
    if not bucket:
        raise ValueError("--s3 缺少 bucket")
    return bucket, prefix


# ---------- 主循环 ----------

def serve_cycle(transport, log) -> bool:
    """领一个任务并完成；返回是否有任务。"""
    job = transport.claim()
    if not job:
        return False
    log(f"job {job['job_id']}: {len(job['ips'])} targets, ping={job['ping_count']} tcp={job['tcp_ports']}x{job['tcp_count']}")
    items = probe_targets(job["ips"], job["ping_count"], job["tcp_ports"], job["tcp_count"])
    transport.submit(job["job_id"], items)
    log(f"job {job['job_id']}: submitted {len(items)} results")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="crossborder-agent：任意机器上的出向探测 agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("once", help="一次性探测指定目标并打印结果")
    once.add_argument("--targets", required=True, help="逗号分隔的 IP 列表")
    once.add_argument("--ports", default="443", help="逗号分隔的 TCP 端口，默认 443")
    once.add_argument("--ping-count", type=int, default=10)
    once.add_argument("--tcp-count", type=int, default=5)
    srv = sub.add_parser("serve", help="轮询任务并回传结果")
    src = srv.add_mutually_exclusive_group(required=True)
    src.add_argument("--server", help="选择器 HTTP 地址，如 http://127.0.0.1:8766")
    src.add_argument("--s3", help="S3 信箱，如 s3://bucket/crossborder-agent")
    srv.add_argument("--token", default="", help="HTTP 传输的 Bearer token")
    srv.add_argument("--agent-id", required=True, help="本 agent 的唯一 ID，如 bj-telecom-01")
    srv.add_argument("--isp", default="", help="本机出口运营商标签：telecom / unicom / mobile 或自由文本")
    srv.add_argument("--profile", default=None, help="S3 模式的 AWS profile")
    srv.add_argument("--region", default=None, help="S3 模式的 bucket 区域")
    srv.add_argument("--interval", type=float, default=15.0, help="轮询间隔秒数")
    srv.add_argument("--once", action="store_true", help="只跑一个轮询周期后退出")
    args = p.parse_args(argv)

    def log(msg):
        print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)

    if args.cmd == "once":
        ips = [s.strip() for s in args.targets.split(",") if s.strip()]
        ports = [int(s) for s in args.ports.split(",") if s.strip()]
        items = probe_targets(ips, args.ping_count, ports, args.tcp_count)
        print(MARKER + json.dumps(items, ensure_ascii=False))
        return 0

    public_ip = detect_public_ip() or ""
    if args.server:
        transport = HttpTransport(args.server, args.token, args.agent_id, args.isp, public_ip=public_ip)
    else:
        bucket, prefix = parse_s3_url(args.s3)
        transport = S3Transport(make_s3_client(args.profile, args.region), bucket, prefix, args.agent_id, args.isp,
                                public_ip=public_ip)
    log(f"agent {args.agent_id} (isp={args.isp or '-'}, public_ip={public_ip or '未知'}) on {_platform.platform()} "
        f"→ {args.server or args.s3}")
    while True:
        try:
            had_job = serve_cycle(transport, log)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"transport error: {e}")
            had_job = False
        if args.once:
            return 0
        time.sleep(args.interval if not had_job else 1.0)


if __name__ == "__main__":
    sys.exit(main())
