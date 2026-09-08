import json
import threading
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
    server = make_server("127.0.0.1", 0, api, mgr, demo=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, mgr
    server.shutdown()
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


def test_index_and_meta(srv):
    base, _ = srv
    status, body = _get(base + "/", raw=True)
    assert status == 200 and b"<html" in body.lower() and "向导".encode() in body
    assert _get(base + "/api/meta")[1]["demo"] is True


def test_env_options_plan(srv):
    base, _ = srv
    assert _get(base + "/api/env?region=ap-east-1")[1]["ok"] is True
    assert _get(base + "/api/options?region=ap-east-1")[1]["instance_types"]
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
    st, sel = _post(base + f"/api/runs/{rid}/select", {"instance_id": winners[0], "protect": True, "terminate_others": True})
    assert st == 200 and sel["selected"] == winners[0] and sel["terminated"] == winners[1:]
    st, bad = _post(base + f"/api/runs/{rid}/select", {"instance_id": "i-nope", "protect": False, "terminate_others": False})
    assert st == 400
    st, cl = _post(base + "/api/cleanup", {"run_id": rid})
    assert st == 200 and cl["run_id"] == rid


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
