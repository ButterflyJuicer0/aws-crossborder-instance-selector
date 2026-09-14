"""标准库 HTTP 服务：路由、JSON 编解码、SSE。业务逻辑在 api.py / runs.py。"""
import json
import os
import queue
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlsplit, parse_qs

from crossborder_selector.web.api import ApiError
from crossborder_selector.aws.catalog import region_catalog

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9][A-Za-z0-9-]*)(?:/(events|cancel|select|terminate_others|report\.(json|md|csv)))?$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _hostname(value, has_scheme=False):
    """从 Host 头（host[:port] 或 [ipv6]:port）或 Origin（完整 URL）中取主机名，小写、去括号。"""
    if not value:
        return None
    try:
        return urlsplit(value if has_scheme else "//" + value).hostname
    except ValueError:
        return None


def _is_loopback(hostname):
    return hostname is not None and hostname.lower() in _LOOPBACK


class Handler(BaseHTTPRequestHandler):
    server_version = "crossborder-web/1.0"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "未找到"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(400, "请求体不是合法 JSON")

    @property
    def ctx(self):
        return self.server.ctx

    def _selection(self, run_id):
        path = os.path.join(self.ctx["runs"].output_dir, run_id, "selection.json")
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save_selection(self, run_id, selection):
        path = os.path.join(self.ctx["runs"].output_dir, run_id, "selection.json")
        temporary = path + ".tmp"
        with open(temporary, "w") as file:
            json.dump(selection, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    def _region_for(self, run_id):
        rec = self.ctx["runs"].get(run_id)
        if rec:
            return rec.region
        path = os.path.join(self.ctx["runs"].output_dir, run_id, "status.json")
        try:
            with open(path) as f:
                return json.load(f).get("region")
        except (OSError, ValueError):
            raise ApiError(404, "run 不存在")

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route(self.command)  # self.command 为实际方法（GET/POST），do_POST 复用本函数
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # 兜底：不让线程静默死掉
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    do_POST = do_GET

    def _method_not_allowed(self):
        self._json(405, {"error": "方法不支持"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    def _guard(self, method):
        """本机安全护栏：非回环 Host 拒绝（--allow-remote 放开）；POST 强制 JSON 且拒绝跨站 Origin。"""
        if not self.ctx.get("allow_remote"):
            if not _is_loopback(_hostname(self.headers.get("Host"))):
                raise ApiError(403, "非本机访问被拒绝")
        if method == "POST":
            ct = self.headers.get("Content-Type") or ""
            if not ct.startswith("application/json"):
                raise ApiError(415, "Content-Type 必须为 application/json")
            origin = self.headers.get("Origin")
            if origin:
                try:
                    actual = urlsplit(origin)
                    expected = urlsplit("http://" + self.headers.get("Host", ""))
                    same_origin = (actual.scheme == expected.scheme and
                                   actual.hostname == expected.hostname and
                                   (actual.port or 80) == (expected.port or 80) and
                                   not actual.username and not actual.password)
                except ValueError:
                    same_origin = False
                if not same_origin:
                    raise ApiError(403, "跨站请求被拒绝")

    def _agent_api(self, method, u, qs):
        """agent 领任务/回传结果：agent 在别的机器上，不走本机 Origin 护栏，改用配置里的 Bearer token。
        registry 是页面读取的，走本机护栏、不需要 token。"""
        from crossborder_selector.probes.agent_transport import handle_agent_request, shared_store
        store = shared_store()
        if u.path == "/api/agent/registry" and method == "GET":
            self._guard(method)
            return self._json(200, store.registry())
        token = self.ctx["api"].load({}).backends["agent"]["http"].get("token", "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, obj = handle_agent_request(store, token, method, u.path, qs, self.headers, body)
        if obj is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._json(status, obj)

    def _route(self, method):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path.startswith("/api/agent/"):
            return self._agent_api(method, u, qs)
        self._guard(method)
        api, runs = self.ctx["api"], self.ctx["runs"]
        p = u.path
        if method == "GET" and p == "/":
            return self._file(os.path.join(self.ctx["static_dir"], "index.html"), "text/html; charset=utf-8")
        if method == "GET" and p == "/api/meta":
            return self._json(200, {"demo": self.ctx["demo"], "version": "1.0",
                                    "default_region": api.load({}).region,
                                    **region_catalog(), **api.settings(),
                                    "allow_remote": self.ctx.get("allow_remote", False)})
        if method == "GET" and p == "/api/regions":
            return self._json(200, api.regions())
        if method == "GET" and p == "/api/env":
            return self._json(200, api.env(qs.get("region", ["ap-east-1"])[0]))
        if method == "GET" and p == "/api/options":
            return self._json(200, api.options(qs.get("region", ["ap-east-1"])[0], qs.get("subnet_id", [None])[0],
                                              refresh=qs.get("refresh", ["0"])[0] == "1"))
        if method == "GET" and p == "/api/images":
            return self._json(200, api.images(qs.get("region", ["ap-east-1"])[0],
                                             qs.get("instance_type", ["t3.nano"])[0]))
        if method == "POST" and p == "/api/capacity":
            return self._json(200, api.capacity(api.load(self._body())))
        if method == "POST" and p == "/api/plan":
            return self._json(200, api.plan(self._body()))
        if method == "POST" and p == "/api/runs":
            return self._json(200, {"run_id": runs.start(self._body())})
        if method == "GET" and p == "/api/runs":
            return self._json(200, runs.list())
        if method == "POST" and p == "/api/cleanup":
            body = self._body()
            rid = body.get("run_id") or ""
            if not _RUN_ID_RE.match(rid):  # 与路由同一字符集，杜绝逃逸 output_dir
                raise ApiError(400, "run id 非法")
            return self._json(200, api.cleanup(rid, self._region_for(rid)))
        m = _RUN_RE.match(p)
        if not m:
            raise ApiError(404, "未找到")
        rid, sub, ext = m.group(1), m.group(2), m.group(3)
        if ".." in rid or "/" in rid:  # 防止逃逸 output_dir
            raise ApiError(400, "run id 非法")
        if sub is None and method == "GET":
            rec = runs.get(rid)
            if rec:
                d = rec.to_detail()
                d["selection"] = self._selection(rid)
                return self._json(200, d)
            path = os.path.join(runs.output_dir, rid, "status.json")
            if not os.path.exists(path):
                raise ApiError(404, "run 不存在")
            with open(path) as f:
                d = json.load(f)
            ev_path = os.path.join(runs.output_dir, rid, "events.jsonl")
            d["events"] = []
            if os.path.exists(ev_path):
                with open(ev_path) as f:
                    d["events"] = [json.loads(l) for l in f if l.strip()][-200:]
            if d.get("state") == "running":
                d["state"] = "unknown"
            d["selection"] = self._selection(rid)
            return self._json(200, d)
        if sub == "events" and method == "GET":
            return self._sse(rid)
        if sub == "cancel" and method == "POST":
            if not runs.cancel(rid):
                raise ApiError(404, "run 不存在或已结束")
            return self._json(200, {"run_id": rid, "cancel_requested": True})
        if sub == "select" and method == "POST":
            body = self._body()
            with self.ctx["selection_lock"]:
                previous = self._selection(rid)
                if previous:
                    raise ApiError(409, f"本次运行已选定 {previous.get('selected')}，请刷新查看")
                result = api.select(rid, body.get("instance_id", ""), bool(body.get("protect")), bool(body.get("terminate_others")),
                                    self._region_for(rid), runs.winner_ids(rid))
                self._save_selection(rid, result)
            return self._json(200, result)
        if sub == "terminate_others" and method == "POST":
            with self.ctx["selection_lock"]:
                sel = self._selection(rid)
                if not sel:
                    raise ApiError(409, "本次运行尚未选定，无法终止其余候选")
                already = set(sel.get("terminated", []))
                remaining = [iid for iid in runs.winner_ids(rid) if iid not in already]
                result = api.terminate_others(sel.get("selected", ""), self._region_for(rid), remaining)
                sel["terminated"] = sorted(already | set(result["terminated"]))
                self._save_selection(rid, sel)
            return self._json(200, {**result, "selection": sel})
        if sub and sub.startswith("report.") and method == "GET":
            ctype = {"json": "application/json; charset=utf-8", "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8"}[ext]
            name = {"json": "report.json", "md": "report.md", "csv": "candidates.csv"}[ext]
            return self._file(os.path.join(runs.output_dir, rid, name), ctype)
        raise ApiError(404, "未找到")

    # ---------- SSE ----------
    def _sse(self, rid):
        runs = self.ctx["runs"]
        rec = runs.get(rid)
        if rec is None:
            raise ApiError(404, "run 不存在或服务已重启，请查看历史记录")
        q = runs.subscribe(rid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            replay = list(rec.events)
            for ev in replay:
                self._sse_write(ev)
            if rec.state in ("finished", "failed", "cancelled"):
                return  # 已结束的 run：回放完立即收尾，不再挂起等待
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(ev)
                if ev["type"] in ("finished", "failed"):
                    return
        finally:
            runs.unsubscribe(rid, q)

    def _sse_write(self, ev):
        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()


def make_server(host, port, api, runs, static_dir=None, demo=False, allow_remote=False) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = Server((host, port), Handler)
    server.daemon_threads = True
    server.ctx = {"api": api, "runs": runs, "static_dir": static_dir or STATIC_DIR, "demo": demo,
                  "allow_remote": allow_remote, "selection_lock": threading.Lock()}
    return server
