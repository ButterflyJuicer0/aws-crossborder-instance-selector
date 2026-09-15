"""agent 的远程传输：任务信箱抽象 + HTTP 与 S3 两种实现 + RemoteAgentBackend。

方向始终是 agent 主动出向：领任务（候选 IP 列表）→ 本地探测 → 回传结果。agent 所在机器不需要任何入站。
- HTTP：agent 轮询选择器（CLI 自带监听器，或 Web 服务同一端口），Bearer token 鉴权。
- S3：任务与结果都是 bucket 里的对象，agent 只需出向 HTTPS 与一组限定前缀的凭证。
两种传输下 agent 的 isp 标签由 agent 自报，`backends.agent.instances` 可按 agent_id 覆盖。
"""
import ipaddress
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from crossborder_selector.probes.agent import items_to_probes, merge_agent_results
from crossborder_selector.probes.base import ProbeBackend


def _job(ips, ping_count, tcp_ports, tcp_count, ttl_s, now):
    return {"job_id": f"job-{uuid.uuid4().hex[:12]}", "ips": list(ips), "ping_count": int(ping_count),
            "tcp_ports": [int(p) for p in tcp_ports], "tcp_count": int(tcp_count),
            "created_at": now, "expires_at": now + ttl_s}


class AgentJobStore:
    """内存信箱：CLI 与 Web 服务共用；HTTP 传输的 broker 就是它本身。"""

    def __init__(self, clock=time.time, sleeper=time.sleep):
        self._now, self._sleep = clock, sleeper
        self._jobs, self._results, self._claimed, self._registry = {}, {}, {}, {}
        self._lock = threading.Lock()

    def _expire(self):
        now = self._now()
        for jid in [j for j, job in self._jobs.items() if job["expires_at"] < now]:
            self._jobs.pop(jid, None)
            self._claimed.pop(jid, None)

    def publish(self, ips, ping_count, tcp_ports, tcp_count, ttl_s) -> dict:
        job = _job(ips, ping_count, tcp_ports, tcp_count, ttl_s, self._now())
        with self._lock:
            self._jobs[job["job_id"]] = job
            self._results[job["job_id"]] = {}
            self._claimed[job["job_id"]] = set()
        return job

    def _touch(self, agent_id, isp, remote_addr, public_ip=None):
        entry = self._registry.setdefault(agent_id, {"isp": "", "last_seen": 0.0, "ip": "", "public_ip": ""})
        entry.update({"isp": isp or entry.get("isp", ""), "last_seen": self._now()})
        if remote_addr:  # 连接来源 IP；未带地址的调用不覆盖已有值
            entry["ip"] = remote_addr
        if public_ip:  # agent 自报的公网出口 IP，比连接来源（可能是回环/NAT 内网）更适合做拨测放行
            entry["public_ip"] = public_ip

    def claim(self, agent_id: str, isp: str = "", remote_addr: str = None, public_ip: str = None):
        with self._lock:
            self._expire()
            self._touch(agent_id, isp, remote_addr, public_ip)
            for jid in sorted(self._jobs, key=lambda j: self._jobs[j]["created_at"]):
                if agent_id not in self._claimed[jid]:
                    self._claimed[jid].add(agent_id)
                    return dict(self._jobs[jid])
        return None

    def submit(self, job_id: str, agent_id: str, isp: str, items: list, remote_addr: str = None, public_ip: str = None):
        with self._lock:
            if job_id not in self._results:
                raise KeyError(job_id)
            self._results[job_id][agent_id] = {"isp": isp, "items": list(items), "received_at": self._now(),
                                               "public_ip": public_ip or ""}
            self._touch(agent_id, isp, remote_addr, public_ip)

    def collect(self, job_id: str, min_agents: int, timeout_s: float, poll_s: float = 1.0) -> dict:
        deadline = self._now() + timeout_s
        while True:
            with self._lock:
                got = dict(self._results.get(job_id, {}))
            if len(got) >= min_agents or self._now() >= deadline:
                with self._lock:  # 收齐或超时后撤下任务，避免迟到的 agent 白跑
                    self._jobs.pop(job_id, None)
                    self._claimed.pop(job_id, None)
                return got
            self._sleep(poll_s)

    def registry(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._registry.items()}


_SHARED_STORE = None
_SHARED_LOCK = threading.RLock()  # 可重入：get_or_start_listener 持锁期间会调用 shared_store()


def shared_store() -> AgentJobStore:
    """进程级单例：Web 服务与本进程内的运行共用同一信箱。"""
    global _SHARED_STORE
    with _SHARED_LOCK:
        if _SHARED_STORE is None:
            _SHARED_STORE = AgentJobStore()
        return _SHARED_STORE


def valid_ip(value) -> str:
    """合法 IP 字符串原样返回，否则空串。用于 agent 自报的 public_ip。"""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except (ValueError, TypeError):
        return ""


def handle_agent_request(store: AgentJobStore, token: str, method: str, path: str, query: dict,
                         headers, body: bytes, remote_addr: str = None):
    """与具体 HTTP 服务器解耦的请求处理，返回 (status, json_obj_or_None)。

    Web 服务与独立监听器都调用它，鉴权规则一致：token 非空时必须匹配 Bearer。
    """
    if token:
        auth = headers.get("Authorization", "") if hasattr(headers, "get") else ""
        if auth != f"Bearer {token}":
            return 401, {"error": "unauthorized"}
    if method == "GET" and path == "/api/agent/jobs":
        agent_id = (query.get("agent_id") or [""])[0].strip()
        if not agent_id:
            return 400, {"error": "agent_id required"}
        job = store.claim(agent_id, (query.get("isp") or [""])[0].strip(), remote_addr=remote_addr,
                          public_ip=valid_ip((query.get("public_ip") or [""])[0]))
        return (200, job) if job else (204, None)
    if method == "GET" and path == "/api/agent/registry":
        return 200, store.registry()
    if method == "POST" and path == "/api/agent/results":
        try:
            data = json.loads(body or b"")
            job_id, agent_id = str(data["job_id"]), str(data["agent_id"])
            isp, items = str(data.get("isp") or ""), list(data.get("items") or [])
            public_ip = valid_ip(data.get("public_ip") or "")
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "invalid json body"}
        try:
            store.submit(job_id, agent_id, isp, items, remote_addr=remote_addr, public_ip=public_ip)
        except KeyError:
            return 404, {"error": "unknown or expired job"}
        return 200, {"accepted": True}
    return 404, {"error": "not found"}


class AgentHttpServer:
    """独立监听器（CLI 用）。默认只绑定 127.0.0.1；对外暴露时必须设置 token 并限制来源网络。"""

    def __init__(self, store: AgentJobStore, host: str, port: int, token: str = ""):
        self.store, self.token = store, token
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默访问日志
                pass

            def _serve(self, method):
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, obj = handle_agent_request(outer.store, outer.token, method, u.path, parse_qs(u.query),
                                                   self.headers, body, remote_addr=self.client_address[0])
                payload = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(status)
                if payload:
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_GET(self): self._serve("GET")
            def do_POST(self): self._serve("POST")

        class Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in host and not host.count(".") else socket.AF_INET
            allow_reuse_address = True
        self._server = Server((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


_LISTENERS = {}


def get_or_start_listener(listen: str, token: str, store: AgentJobStore = None) -> AgentHttpServer:
    """按监听地址缓存独立监听器，多次运行不重复绑定端口。"""
    store = store or shared_store()  # 在取锁之前拿到信箱，避免嵌套加锁
    with _SHARED_LOCK:
        srv = _LISTENERS.get(listen)
        if srv is None:
            host, _, port = listen.rpartition(":")
            srv = AgentHttpServer(store, host or "127.0.0.1", int(port), token).start()
            _LISTENERS[listen] = srv
        return srv


class S3AgentBroker:
    """S3 信箱：jobs/<job_id>.json 为任务，results/<job_id>/<agent_id>.json 为结果。"""

    def __init__(self, s3_client, bucket: str, prefix: str, clock=time.time, sleeper=time.sleep):
        self.s3, self.bucket = s3_client, bucket
        self.prefix = prefix.strip("/")
        self._now, self._sleep = clock, sleeper

    def _key(self, *parts):
        return "/".join([self.prefix, *parts]) if self.prefix else "/".join(parts)

    def publish(self, ips, ping_count, tcp_ports, tcp_count, ttl_s) -> dict:
        job = _job(ips, ping_count, tcp_ports, tcp_count, ttl_s, self._now())
        self.s3.put_object(Bucket=self.bucket, Key=self._key("jobs", f"{job['job_id']}.json"),
                           Body=json.dumps(job).encode(), ContentType="application/json")
        return job

    def _results(self, job_id):
        out, token = {}, None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._key("results", job_id) + "/"}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                try:
                    data = json.loads(self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read())
                    agent_id = str(data.get("agent_id") or obj["Key"].rsplit("/", 1)[-1].removesuffix(".json"))
                    out[agent_id] = {"isp": str(data.get("isp") or ""), "items": list(data.get("items") or []),
                                     "received_at": obj.get("LastModified"), "public_ip": valid_ip(data.get("public_ip") or "")}
                except (ValueError, KeyError, TypeError):
                    continue  # 单个坏对象不影响其他 agent
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")

    def registry(self) -> dict:
        """读取 agent 写的心跳 registry/<agent_id>.json → 与内存信箱 registry() 同构。"""
        out, token = {}, None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self._key("registry") + "/"}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                try:
                    data = json.loads(self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read())
                    agent_id = str(data.get("agent_id") or obj["Key"].rsplit("/", 1)[-1].removesuffix(".json"))
                    out[agent_id] = {"isp": str(data.get("isp") or ""), "public_ip": valid_ip(data.get("public_ip") or ""),
                                     "ip": "", "last_seen": float(data.get("last_seen") or 0.0)}
                except (ValueError, KeyError, TypeError):
                    continue
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")

    def collect(self, job_id: str, min_agents: int, timeout_s: float, poll_s: float = 5.0) -> dict:
        deadline = self._now() + timeout_s
        while True:
            got = self._results(job_id)
            if len(got) >= min_agents or self._now() >= deadline:
                try:
                    self.s3.delete_object(Bucket=self.bucket, Key=self._key("jobs", f"{job_id}.json"))
                except Exception:
                    pass  # 任务对象会按 expires_at 被 agent 忽略；删除失败不影响结果
                return got
            self._sleep(poll_s)


class RemoteAgentBackend(ProbeBackend):
    """通过任务信箱驱动远程 agent；与 SSM 版共用结果格式、合并与评分。"""
    name = "agent"

    def __init__(self, broker, backend_cfg: dict):
        self.broker, self.cfg = broker, backend_cfg
        self.isp_override = dict(backend_cfg.get("instances") or {})

    def probe(self, candidates: list) -> dict:
        ips = [c.public_ip for c in candidates]
        timeout = float(self.cfg["timeout_s"])
        job = self.broker.publish(ips, self.cfg["ping_count"], self.cfg["tcp_ports"], self.cfg["tcp_count"],
                                  ttl_s=timeout)
        results = self.broker.collect(job["job_id"], int(self.cfg.get("min_agents", 1)), timeout)
        per_agent = []
        for agent_id, res in results.items():
            isp = self.isp_override.get(agent_id) or res.get("isp") or agent_id
            try:
                per_agent.append((f"{agent_id}({isp})", items_to_probes(res.get("items") or [], isp), ""))
            except (ValueError, KeyError, TypeError) as e:
                per_agent.append((f"{agent_id}({isp})", {}, f"parse error: {e}"))
        return merge_agent_results(ips, per_agent, self.name)
