"""标准库 HTTP 服务：路由、JSON 编解码、SSE。业务逻辑在 api.py / runs.py。"""
import json
import os
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from crossborder_selector.web.api import ApiError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9][A-Za-z0-9-]*)(?:/(events|cancel|select|report\.(json|md|csv)))?$")


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

    def _route(self, method):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        api, runs = self.ctx["api"], self.ctx["runs"]
        p = u.path
        if method == "GET" and p == "/":
            return self._file(os.path.join(self.ctx["static_dir"], "index.html"), "text/html; charset=utf-8")
        if method == "GET" and p == "/api/meta":
            return self._json(200, {"demo": self.ctx["demo"], "version": "1.0"})
        if method == "GET" and p == "/api/env":
            return self._json(200, api.env(qs.get("region", ["ap-east-1"])[0]))
        if method == "GET" and p == "/api/options":
            return self._json(200, api.options(qs.get("region", ["ap-east-1"])[0]))
        if method == "POST" and p == "/api/plan":
            return self._json(200, api.plan(self._body()))
        if method == "POST" and p == "/api/runs":
            return self._json(200, {"run_id": runs.start(self._body())})
        if method == "GET" and p == "/api/runs":
            return self._json(200, runs.list())
        if method == "POST" and p == "/api/cleanup":
            body = self._body()
            rid = body.get("run_id") or ""
            if ".." in rid or "/" in rid:  # 防止逃逸 output_dir
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
                return self._json(200, rec.to_detail())
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
            return self._json(200, d)
        if sub == "events" and method == "GET":
            return self._sse(rid)
        if sub == "cancel" and method == "POST":
            if not runs.cancel(rid):
                raise ApiError(404, "run 不存在或已结束")
            return self._json(200, {"run_id": rid, "cancel_requested": True})
        if sub == "select" and method == "POST":
            body = self._body()
            result = api.select(rid, body.get("instance_id", ""), bool(body.get("protect")), bool(body.get("terminate_others")),
                                self._region_for(rid), runs.winner_ids(rid))
            with open(os.path.join(runs.output_dir, rid, "selection.json"), "w") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            return self._json(200, result)
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
            if replay and replay[-1]["type"] in ("finished", "failed"):
                return
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


def make_server(host, port, api, runs, static_dir=None, demo=False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.ctx = {"api": api, "runs": runs, "static_dir": static_dir or STATIC_DIR, "demo": demo}
    return server
