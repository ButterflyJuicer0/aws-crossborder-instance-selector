import json
import os
import queue
import threading
import time

from crossborder_selector.models import Candidate, CandidateScore, RoundResult, RunResult
from crossborder_selector.web.runs import RunManager


class FakeApi:
    """只提供 factory 与 load；不触碰 AWS。"""
    def __init__(self, cfg):
        self._cfg = cfg
        self.factory = lambda c: {"ec2": FakeEc2(), "iam": object(), "ssm": object()}
    def load(self, overrides):
        return self._cfg


class FakeEc2:
    # 真实 clients["ec2"] 是原始 boto3 client，实现里用 Ec2Manager 包装后调用 list_run_instances，
    # 其内部走 describe_instances；故假 client 需提供 describe_instances 而非 list_run_instances。
    def describe_instances(self, **kwargs):
        return {"Reservations": [{"Instances": [{"InstanceId": "i-left"}]}]}


def _score(iid, ip, comp):
    return CandidateScore(Candidate(iid, ip, "18.162.0.0/16", 1, "t"), None, [], {}, {}, comp, True, "")


class FakeOrch:
    """模拟一次两轮运行，发出真实事件序列。"""
    behaviour = "ok"
    def __init__(self, cfg, ec2, ssm, backends, sources, prefix_lookup, infra, run_id, log, on_event, should_stop, **kw):
        self.run_id, self.log, self.on_event, self.should_stop, self.cfg = run_id, log, on_event, should_stop, cfg
    def run(self):
        if FakeOrch.behaviour == "boom":
            raise RuntimeError("ssm exploded")
        self.log("[round 1] launching")
        self.on_event({"type": "round_started", "round": 1, "batch_size": 2})
        self.on_event({"type": "round_done", "round": 1, "kept": [{"instance_id": "i-1", "public_ip": "10.0.0.1", "composite": 88.0}], "terminated": ["i-2"], "backend_errors": {}})
        stop = "cancelled" if self.should_stop() else "max_rounds"
        w = _score("i-1", "10.0.0.1", 88.0)
        return RunResult(self.run_id, self.cfg.region, [RoundResult(1, [w.candidate], [], [w], [w], ["i-2"], {})], [w], "t0", "t1", stop)


def _patched(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr("crossborder_selector.web.runs.ensure_infra", lambda ec2, iam, ssm, c: object())
    monkeypatch.setattr("crossborder_selector.web.runs.build_backends", lambda c, s: [])
    monkeypatch.setattr("crossborder_selector.web.runs.build_sources", lambda c: [])
    monkeypatch.setattr("crossborder_selector.web.runs.load_ip_ranges", lambda cache_path=None: [])
    monkeypatch.setattr("crossborder_selector.web.runs.SsmRunner", lambda client: object())
    return RunManager(FakeApi(cfg), str(tmp_path / "out"), orchestrator_factory=FakeOrch,
                      history_file=str(tmp_path / "hist.json"))


def _wait(mgr, run_id, states=("finished", "failed", "cancelled"), timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if mgr.get(run_id).state in states:
            return mgr.get(run_id)
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_start_runs_and_persists(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    q = None
    run_id = mgr.start({"region": "ap-east-1"})
    assert run_id.startswith("xb-")
    rec = _wait(mgr, run_id)
    assert rec.state == "finished" and rec.stop_reason == "max_rounds"
    types = [e["type"] for e in rec.events]
    assert types == ["log", "round_started", "round_done", "finished"]
    assert all("ts" in e for e in rec.events)
    assert rec.winners[0]["instance_id"] == "i-1" and rec.report_paths["json"].endswith("report.json")
    assert os.path.exists(rec.report_paths["json"])
    with open(tmp_path / "out" / run_id / "status.json") as f:
        st = json.load(f)
    assert st["state"] == "finished" and "events" not in st
    with open(tmp_path / "out" / run_id / "events.jsonl") as f:
        lines = f.read().splitlines()
    assert len(lines) == 4 and json.loads(lines[-1])["type"] == "finished"
    assert mgr.winner_ids(run_id) == ["i-1"]
    assert "api_key" not in json.dumps(rec.config)


def test_run_cfg_disables_protect_but_record_keeps_choice(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1", "protect": True}))
    captured = {}
    class CapOrch(FakeOrch):
        def __init__(self, cfg, *a, **kw):
            captured["protect"] = cfg.protect
            super().__init__(cfg, *a, **kw)
    mgr.orchestrator_factory = CapOrch
    run_id = mgr.start({"region": "ap-east-1", "protect": True})
    rec = _wait(mgr, run_id)
    assert captured["protect"] is False           # 运行阶段不加固任何实例
    assert rec.config["protect"] is True          # 用户的 protect 选择被保留在 record.config


def test_history_file_override_isolates_repo_history(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    monkeypatch.chdir(tmp_path)
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    run_id = mgr.start({"region": "ap-east-1"})
    _wait(mgr, run_id)
    assert os.path.exists(tmp_path / "hist.json")
    assert not os.path.exists(tmp_path / "history" / "prefix_stats.json")


def test_subscribe_receives_live_events(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    # 用 threading.Event 门控 orchestrator：先订阅、再放行，确保能收到实时事件（不再依赖竞态）
    gate = threading.Event()
    class GatedOrch(FakeOrch):
        def run(self):
            gate.wait(5)
            return super().run()
    mgr.orchestrator_factory = GatedOrch
    run_id = mgr.start({"region": "ap-east-1"})
    q = mgr.subscribe(run_id)
    gate.set()
    got = []
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            ev = q.get(timeout=0.2)
        except queue.Empty:
            continue
        got.append(ev["type"])
        if ev["type"] in ("finished", "failed"):
            break
    assert got[-1] == "finished"
    mgr.unsubscribe(run_id, q)


def test_failure_records_leftovers(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "boom"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    run_id = mgr.start({"region": "ap-east-1"})
    rec = _wait(mgr, run_id)
    assert rec.state == "failed" and "ssm exploded" in rec.error
    assert rec.events[-1]["type"] == "failed" and rec.events[-1]["leftover_instance_ids"] == ["i-left"]
    FakeOrch.behaviour = "ok"


def test_report_write_failure_keeps_winners(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("crossborder_selector.web.runs.write_reports", boom)
    run_id = mgr.start({"region": "ap-east-1"})
    rec = _wait(mgr, run_id)
    assert rec.state == "failed" and "disk full" in rec.error
    assert rec.winners and rec.winners[0]["instance_id"] == "i-1"       # winner 已在报告失败前落库
    assert rec.events[-1]["type"] == "failed" and rec.events[-1]["winner_instance_ids"] == ["i-1"]


def test_cancel_sets_flag_and_state(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    gate = threading.Event()
    class SlowOrch(FakeOrch):
        def run(self):
            gate.wait(5)
            return super().run()
    mgr.orchestrator_factory = SlowOrch
    run_id = mgr.start({"region": "ap-east-1"})
    mgr.cancel(run_id)
    gate.set()
    rec = _wait(mgr, run_id)
    assert rec.state == "cancelled" and rec.stop_reason == "cancelled"
    assert "cancelled" in [e["type"] for e in rec.events]


def test_cancel_rejected_after_run_finished(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    run_id = mgr.start({"region": "ap-east-1"})
    rec = _wait(mgr, run_id)
    assert rec.state == "finished"
    assert mgr.cancel(run_id) is False  # 已结束的 run 不能取消


def test_list_marks_stale_running_as_unknown(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    out = tmp_path / "out" / "xb-old"
    out.mkdir(parents=True)
    (out / "status.json").write_text(json.dumps({"run_id": "xb-old", "state": "running", "region": "ap-east-1", "started_at": "t"}))
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    rows = mgr.list()
    assert rows[0]["run_id"] == "xb-old" and rows[0]["state"] == "unknown"
