import json
import threading
import time
import urllib.request
import urllib.error

import pytest

from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import demo_factory, install_demo
from crossborder_selector.web.runs import RunManager
from crossborder_selector.web.server import make_server


@pytest.fixture
def srv(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    mgr = RunManager(api, str(tmp_path / "out"))
    restore = install_demo(mgr, step_delay=0)
    server = None
    try:
        server = make_server("127.0.0.1", 0, api, mgr, demo=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield base, mgr
    finally:
        if server:
            server.shutdown()
            server.server_close()
        restore()


def _get(url, raw=False):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
        return (r.status, body) if raw else (r.status, json.loads(body))


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _req(url, method="POST", headers=None, data=b"{}"):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_index_and_meta(srv):
    base, _ = srv
    status, body = _get(base + "/", raw=True)
    assert status == 200 and b"<html" in body.lower() and b'id="content"' in body
    meta = _get(base + "/api/meta")[1]
    assert meta["demo"] is True and meta["default_region"] == "ap-east-1"


def test_env_options_plan(srv):
    base, _ = srv
    assert _get(base + "/api/env?region=ap-east-1")[1]["ok"] is True
    assert _get(base + "/api/options?region=ap-east-1")[1]["instance_types"]
    images = _get(base + "/api/images?region=ap-east-1&instance_type=t4g.nano")[1]
    assert len(images["images"]) == 3
    assert all(i["architecture"] == "arm64" for i in images["images"])
    st, p = _post(base + "/api/plan", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1})
    assert st == 200 and "3 x" in p["plan_summary"]
    st, e = _post(base + "/api/plan", {"batch_size": 0})
    assert st == 400 and "error" in e


def test_run_lifecycle_and_sse(srv):
    base, mgr = srv
    st, r = _post(base + "/api/runs", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1, "keep_top_k": 2})
    assert st == 200 and r["run_id"].startswith("xb-")
    rid = r["run_id"]
    with urllib.request.urlopen(base + f"/api/runs/{rid}/events", timeout=10) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        seen = []
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                seen.append(ev["type"])
                if ev["type"] in ("finished", "failed"):
                    break
    assert seen[-1] == "finished" and "round_done" in seen
    st, detail = _get(base + f"/api/runs/{rid}")
    assert detail["state"] == "finished" and len(detail["winners"]) == 2
    assert _get(base + "/api/runs")[1][0]["run_id"] == rid
    st, body = _get(base + f"/api/runs/{rid}/report.csv", raw=True)
    assert st == 200 and body.startswith(b"run_id,")
    winners = [w["instance_id"] for w in detail["winners"]]
    # 坏实例（尚无 selection.json）→ 400
    st, bad = _post(base + f"/api/runs/{rid}/select", {"instance_id": "i-nope", "protect": False, "terminate_others": False})
    assert st == 400
    # 首次选定 → 200
    st, sel = _post(base + f"/api/runs/{rid}/select", {"instance_id": winners[0], "protect": True, "terminate_others": True})
    assert st == 200 and sel["selected"] == winners[0] and sel["terminated"] == winners[1:]
    # 重复选定（无 force）→ 409（I4）
    st, dup = _post(base + f"/api/runs/{rid}/select", {"instance_id": winners[0], "protect": False, "terminate_others": False})
    assert st == 409 and dup["error"]
    # GET 详情带回 selection
    assert _get(base + f"/api/runs/{rid}")[1]["selection"]["selected"] == winners[0]
    st, cl = _post(base + "/api/cleanup", {"run_id": rid})
    assert st == 200 and cl["run_id"] == rid and cl["terminated"] == []


def test_cancel_finished_run_404_and_sse_replays_terminal(srv):
    base, _ = srv
    rid = _post(base + "/api/runs", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1, "keep_top_k": 1})[1]["run_id"]
    detail = {}
    for _ in range(300):
        detail = _get(base + f"/api/runs/{rid}")[1]
        if detail["state"] != "running":
            break
        time.sleep(0.02)
    assert detail["state"] == "finished"
    # 对已结束 run 取消 → 404（RunManager.cancel 返回 False）
    st, b = _post(base + f"/api/runs/{rid}/cancel", {})
    assert st == 404 and b["error"]
    # 已结束 run 的 SSE：回放里带 finished 终态，立即可读到（不必等待新事件）
    seen = []
    with urllib.request.urlopen(base + f"/api/runs/{rid}/events", timeout=5) as resp:
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                seen.append(ev["type"])
                if ev["type"] in ("finished", "failed"):
                    break
    assert seen and seen[-1] == "finished"


def test_terminate_others_endpoint(srv):
    base, _ = srv
    rid = _post(base + "/api/runs", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1, "keep_top_k": 2})[1]["run_id"]
    detail = {}
    for _ in range(300):
        detail = _get(base + f"/api/runs/{rid}")[1]
        if detail["state"] != "running":
            break
        time.sleep(0.02)
    assert detail["state"] == "finished"
    winners = [w["instance_id"] for w in detail["winners"]]
    assert len(winners) == 2
    # 未选定 → 409
    st, b = _post(base + f"/api/runs/{rid}/terminate_others", {})
    assert st == 409 and b["error"]
    # 选定但不终止其余
    st, sel = _post(base + f"/api/runs/{rid}/select", {"instance_id": winners[0], "protect": False, "terminate_others": False})
    assert st == 200 and sel["terminated"] == []
    # 之后单独终止其余保留候选
    st, r = _post(base + f"/api/runs/{rid}/terminate_others", {})
    assert st == 200 and r["terminated"] == winners[1:]
    refreshed = _get(base + f"/api/runs/{rid}")[1]
    assert refreshed["selection"]["terminated"] == winners[1:]
    st, again = _post(base + f"/api/runs/{rid}/terminate_others", {})
    assert st == 200 and again["terminated"] == []
    assert again["selection"]["terminated"] == winners[1:]


def test_cleanup_rejects_invalid_run_id(srv):
    base, _ = srv
    for rid in ["a b", "a.b", "-x", "", "x/y"]:
        st, b = _post(base + "/api/cleanup", {"run_id": rid})
        assert st == 400 and b["error"]


def test_unsupported_methods_return_405_json(srv):
    base, _ = srv
    for method in ("PUT", "DELETE"):
        st, b = _req(base + "/api/runs", method=method, headers={"Content-Type": "application/json"}, data=b"{}")
        assert st == 405 and json.loads(b)["error"] == "方法不支持"


def test_post_guard_content_type_origin_and_host(srv):
    base, _ = srv
    url = base + "/api/plan"
    body = json.dumps({"region": "ap-east-1", "batch_size": 3, "max_rounds": 1}).encode()
    # 正常 JSON POST 仍 200
    st, _ = _req(url, headers={"Content-Type": "application/json"}, data=body)
    assert st == 200
    # 缺 application/json（text/plain）→ 415
    st, b = _req(url, headers={"Content-Type": "text/plain"}, data=body)
    assert st == 415 and json.loads(b)["error"]
    # 跨站 Origin → 403
    st, b = _req(url, headers={"Content-Type": "application/json", "Origin": "http://evil.example.com"}, data=body)
    assert st == 403 and json.loads(b)["error"]
    # 同源（loopback）Origin → 放行
    st, _ = _req(url, headers={"Content-Type": "application/json", "Origin": base}, data=body)
    assert st == 200
    st, _ = _req(url, headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:1"}, data=body)
    assert st == 403
    # 非回环 Host（任意请求）→ 403
    st, b = _req(base + "/api/meta", method="GET", headers={"Host": "evil.example.com"}, data=None)
    assert st == 403 and json.loads(b)["error"]


def test_404_and_bad_json(srv):
    base, _ = srv
    try:
        urllib.request.urlopen(base + "/api/nope", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 404 and json.loads(e.read())["error"]
    req = urllib.request.Request(base + "/api/plan", data=b"{bad", headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_run_id_traversal_rejected(srv, tmp_path):
    base, _ = srv
    # GET 报表下载：含 .. 的 run id 必须被拒（4xx + JSON error），不得读到 out/ 之外
    try:
        urllib.request.urlopen(base + "/api/runs/../report.csv", timeout=5)
        assert False, "应当拒绝 .. run id"
    except urllib.error.HTTPError as e:
        assert 400 <= e.code < 500 and json.loads(e.read()).get("error")
    # POST select：含 .. 的 run id 必须被拒，且不得写出 selection.json
    st, _ = _post(base + "/api/runs/../select", {"instance_id": "i-x", "protect": False, "terminate_others": False})
    assert 400 <= st < 500
    # POST cleanup：body 里的 .. run id 必须被拒
    st, _ = _post(base + "/api/cleanup", {"run_id": ".."})
    assert 400 <= st < 500
    # 未发生目录逃逸写入（out/../selection.json 即 tmp_path/selection.json）
    assert not (tmp_path / "selection.json").exists()
