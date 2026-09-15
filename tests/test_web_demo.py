import json
from crossborder_selector.config import load_config
from crossborder_selector.report import write_reports
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import DemoOrchestrator, demo_factory, install_demo
from crossborder_selector.web.runs import RunManager


def test_demo_orchestrator_emits_events_and_reports(tmp_path):
    cfg = load_config(None, {"batch_size": 4, "max_rounds": 2, "keep_top_k": 2, "target_score": 99,
                             "output_dir": str(tmp_path), "history_file": str(tmp_path / "hist.json")})
    events = []
    orch = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-demo", log=lambda m: None,
                            on_event=events.append, should_stop=lambda: False, step_delay=0)
    rr = orch.run()
    assert [e["type"] for e in events][:3] == ["round_started", "candidates", "vetoed"]
    assert len(rr.rounds) == 2 and len(rr.winners) == 2 and rr.stop_reason == "max_rounds"
    vetoes = {v.veto_reason for v in rr.rounds[0].vetoed}
    assert {"reputation", "reverse_unreachable"} <= vetoes
    paths = write_reports(rr, cfg, out_dir=str(tmp_path))
    with open(paths["json"]) as f:
        d = json.load(f)
    assert d["winners"][0]["reverse_telecom"] is not None and d["candidates"]


def test_demo_is_deterministic_and_stoppable():
    cfg = load_config(None, {"batch_size": 3, "max_rounds": 3, "keep_top_k": 1})
    a = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=lambda: False, step_delay=0).run()
    b = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=lambda: False, step_delay=0).run()
    assert a.winners[0].composite == b.winners[0].composite
    calls = {"n": 0}
    def stop():
        calls["n"] += 1
        return calls["n"] > 1
    c = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=stop, step_delay=0).run()
    assert c.stop_reason == "cancelled" and len(c.rounds) == 1


def test_demo_factory_serves_api_env_and_options(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["ok"] and e["caller"]["account"] == "123456789012"
    assert api.capacity(api.load({}))["vcpu_quota"] == 64.0
    o = api.options("ap-east-1")
    assert len(o["instance_types"]) >= 9


def test_install_demo_runs_end_to_end(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    mgr = RunManager(api, str(tmp_path / "out"))
    restore = install_demo(mgr, step_delay=0)
    try:
        run_id = mgr.start({"region": "ap-east-1", "batch_size": 3, "max_rounds": 1})
        import time
        for _ in range(200):
            if mgr.get(run_id).state != "running":
                break
            time.sleep(0.02)
        rec = mgr.get(run_id)
        assert rec.state == "finished" and rec.winners and (tmp_path / "out" / run_id / "report.md").exists()
    finally:
        restore()


def test_main_rejects_non_loopback_without_allow_remote(monkeypatch):
    from crossborder_selector.web import __main__ as web_main
    calls = {"n": 0}
    monkeypatch.setattr(web_main, "make_server", lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1))
    assert web_main.main(["--host", "0.0.0.0", "--no-browser", "--port", "0"]) == 2
    assert calls["n"] == 0  # 在建服务之前就已拒绝


def test_port_in_use_gives_actionable_message(capsys):
    import socket
    from crossborder_selector.web import __main__ as web_main
    holder = socket.socket(); holder.bind(("127.0.0.1", 0)); holder.listen(1)
    port = holder.getsockname()[1]
    try:
        rc = web_main.main(["--demo", "--port", str(port), "--no-browser"])
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert rc == 2
    assert f"{port}" in err and "已被占用" in err and "--port" in err and "lsof" in err
