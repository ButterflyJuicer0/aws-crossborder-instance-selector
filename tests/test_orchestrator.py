import pytest
from crossborder_selector.aws.infra import Infra
from crossborder_selector.config import load_config
from crossborder_selector.models import IspProbe, ProbeResult, SourceResult
from crossborder_selector.orchestrator import Orchestrator
from crossborder_selector.probes.base import ProbeBackend
from crossborder_selector.reputation.base import ReputationSource

INFRA = Infra("subnet-1", "sg-1", "prof", "ami-1")


class FakeEc2:
    """按脚本分配 IP；记录终止与 winner 操作。"""
    def __init__(self, ips):
        self.ips, self.n = list(ips), 0
        self.live, self.terminated, self.winners, self.protected = {}, [], [], []
    def launch(self, n, run_id, round_no, infra, instance_type):
        ids = []
        for _ in range(n):
            self.n += 1
            iid = f"i-{self.n}"
            self.live[iid] = self.ips.pop(0)
            ids.append(iid)
        return ids
    def wait_running(self, ids): pass
    def public_ips(self, ids): return {i: self.live[i] for i in ids}
    def terminate(self, ids):
        for i in ids:
            self.terminated.append(i); self.live.pop(i, None)
    def mark_winner(self, iid, run_id, score, round_no, now): self.winners.append((iid, score, round_no))
    def protect(self, iid): self.protected.append(iid)


class FakeSsm:
    def __init__(self, offline=()): self.offline = set(offline)
    def wait_online(self, ids, timeout_s): return {i for i in ids if i not in self.offline}


class ScriptedBackend(ProbeBackend):
    """按 IP 给定 rtt；None 表示全丢包。"""
    name = "reverse"
    def __init__(self, rtts): self.rtts = rtts
    def probe(self, candidates):
        out = {}
        for c in candidates:
            rtt = self.rtts.get(c.public_ip, 100.0)
            rx = 0 if rtt is None else 4
            out[c.public_ip] = ProbeResult("reverse", [IspProbe(i, 4, rx, rtt) for i in ("telecom", "unicom", "mobile")])
        return out


class Denylist(ReputationSource):
    name = "dnsbl"; weight = 50.0
    def __init__(self, bad): self.bad = set(bad)
    def check(self, a): return SourceResult(self.name, a in self.bad)


def _cfg(**over):
    return load_config(None, {"batch_size": 2, "max_rounds": 3, "keep_top_k": 1, "target_score": 95,
                              "disable_backends": ["globalping"], **over})


def test_vetoed_terminated_before_probe_and_winner_kept():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(), [ScriptedBackend({"10.0.0.1": 100.0, "10.0.0.2": 100.0})],
                        [Denylist(["10.0.0.2"])], lambda ip: "10.0.0.0/8", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    r1 = rr.rounds[0]
    assert [v.candidate.public_ip for v in r1.vetoed] == ["10.0.0.2"]
    assert r1.vetoed[0].veto_reason == "reputation"
    assert "i-2" in ec2.terminated and "i-1" not in ec2.terminated
    assert rr.winners[0].candidate.public_ip == "10.0.0.1" and rr.winners[0].candidate.prefix == "10.0.0.0/8"
    assert ec2.winners == [("i-1", rr.winners[0].composite, 1)]
    assert rr.stop_reason == "max_rounds" and ec2.protected == []


def test_tournament_replaces_incumbent_and_stops_on_target():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5", "10.0.0.6"])
    be = ScriptedBackend({"10.0.0.1": 200.0, "10.0.0.2": 150.0, "10.0.0.3": 50.0, "10.0.0.4": 180.0})
    orch = Orchestrator(_cfg(), ec2, FakeSsm(), [be], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    assert len(rr.rounds) == 2 and rr.stop_reason == "target_score_reached"
    assert rr.rounds[0].kept[0].candidate.public_ip == "10.0.0.2"
    assert rr.rounds[1].kept[0].candidate.public_ip == "10.0.0.3"
    assert "i-2" in rr.rounds[1].terminated          # 上一轮在位者被替换后终止
    assert set(ec2.live) == {"i-3"}
    assert rr.winners[0].candidate.instance_id == "i-3"


def test_high_score_does_not_stop_before_desired_retention_count_is_met():
    ec2 = FakeEc2([f"10.0.0.{i}" for i in range(1, 10)])
    cfg = _cfg(batch_size=3, max_rounds=3, keep_top_k=7, target_score=0)
    result = Orchestrator(cfg, ec2, FakeSsm(), [ScriptedBackend({})], [], lambda _: "", INFRA,
                          "xb-retain", log=lambda _: None).run()
    assert len(result.rounds) == 3
    assert [len(r.launched) for r in result.rounds] == [3, 3, 3]
    assert len(result.winners) == 7
    assert result.stop_reason == "target_score_reached"


def test_offline_ssm_marks_candidate_and_min_backends_veto():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    class Offline(ProbeBackend):
        name = "reverse"
        def probe(self, cands):
            return {c.public_ip: (ProbeResult("reverse", [], "ssm offline") if not c.ssm_online
                                  else ProbeResult("reverse", [IspProbe(isp, 4, 4, 30.0) for isp in ("telecom", "unicom", "mobile")])) for c in cands}
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(offline=["i-1"]), [Offline()], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    scored = {s.candidate.instance_id: s for s in rr.rounds[0].scored}
    assert scored["i-1"].veto_reason == "reverse_unavailable" and scored["i-2"].qualified
    assert rr.winners[0].candidate.instance_id == "i-2"


def test_protect_flag_and_exception_cleanup():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    orch = Orchestrator(_cfg(max_rounds=1, protect=True), ec2, FakeSsm(), [ScriptedBackend({})], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    orch.run()
    assert ec2.protected == ["i-1"]

    class Boom(ProbeBackend):
        name = "reverse"
        def probe(self, cands): raise RuntimeError("x")
    ec2b = FakeEc2(["10.0.0.1", "10.0.0.2"])
    orch = Orchestrator(_cfg(max_rounds=1), ec2b, FakeSsm(), [Boom()], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    assert rr.winners == [] and rr.stop_reason == "no_qualified"
    assert set(ec2b.terminated) == {"i-1", "i-2"}

    class Ssmboom:
        def wait_online(self, ids, t): raise RuntimeError("ssm api down")
    ec2c = FakeEc2(["10.0.0.1", "10.0.0.2"])
    orch = Orchestrator(_cfg(max_rounds=1), ec2c, Ssmboom(), [ScriptedBackend({})], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    with pytest.raises(RuntimeError):
        orch.run()
    assert set(ec2c.terminated) == {"i-1", "i-2"}


def test_keyboard_interrupt_terminates_all_and_propagates():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])

    class Interrupt(ProbeBackend):
        name = "reverse"
        def probe(self, cands):
            raise KeyboardInterrupt()
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(), [Interrupt()], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        orch.run()
    assert set(ec2.terminated) == {"i-1", "i-2"} and ec2.winners == []


def test_launch_failure_in_later_round_keeps_incumbent():
    from botocore.exceptions import ClientError

    class FailingEc2(FakeEc2):
        def launch(self, n, run_id, round_no, infra, instance_type):
            if round_no >= 2:
                raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity",
                                             "Message": "no capacity"}}, "RunInstances")
            return super().launch(n, run_id, round_no, infra, instance_type)
    ec2 = FailingEc2(["10.0.0.1", "10.0.0.2"])
    be = ScriptedBackend({"10.0.0.1": 100.0, "10.0.0.2": 120.0})
    orch = Orchestrator(_cfg(), ec2, FakeSsm(), [be], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    assert rr.stop_reason == "launch_failed" and len(rr.rounds) == 2
    assert "launch" in rr.rounds[1].backend_errors
    # 上一轮在位者被保留为 winner，未被牵连终止
    incumbent = rr.rounds[0].kept[0].candidate.instance_id
    assert rr.winners[0].candidate.instance_id == incumbent
    assert ec2.winners[0][0] == incumbent and incumbent not in ec2.terminated


def test_first_round_launch_failure_reraises():
    from botocore.exceptions import ClientError

    class FailingEc2(FakeEc2):
        def launch(self, n, run_id, round_no, infra, instance_type):
            raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity", "Message": "x"}}, "RunInstances")
    orch = Orchestrator(_cfg(), FailingEc2([]), FakeSsm(), [ScriptedBackend({})], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    with pytest.raises(ClientError):
        orch.run()


def test_empty_public_ip_is_vetoed_before_reputation():
    ec2 = FakeEc2(["", "10.0.0.2"])
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(), [ScriptedBackend({"10.0.0.2": 30.0})],
                        [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    veto = {v.candidate.instance_id: v.veto_reason for v in rr.rounds[0].vetoed}
    assert veto.get("i-1") == "no_public_ip" and "i-1" in ec2.terminated
    assert rr.winners[0].candidate.instance_id == "i-2"


def test_no_survivors_skips_probing():
    ec2 = FakeEc2(["", ""])

    class BoomSsm:
        def wait_online(self, ids, t):
            raise AssertionError("must not wait_online when there are no survivors")

    class BoomBackend(ProbeBackend):
        name = "reverse"
        def probe(self, cands):
            raise AssertionError("must not probe when there are no survivors")
    orch = Orchestrator(_cfg(max_rounds=1), ec2, BoomSsm(), [BoomBackend()], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    assert rr.stop_reason == "no_qualified" and rr.winners == []
    assert rr.rounds[0].scored == [] and set(ec2.terminated) == {"i-1", "i-2"}


def test_on_event_sequence_and_payloads():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    events = []
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(), [ScriptedBackend({"10.0.0.1": 100.0})],
                        [Denylist(["10.0.0.2"])], lambda ip: "10.0.0.0/8", INFRA, "xb-t",
                        log=lambda *a: None, on_event=events.append)
    orch.run()
    types = [e["type"] for e in events]
    assert types == ["round_started", "candidates", "vetoed", "round_done"]
    assert all("ts" in e and e["round"] == 1 for e in events)
    assert events[0]["batch_size"] == 2
    assert {i["public_ip"] for i in events[1]["items"]} == {"10.0.0.1", "10.0.0.2"}
    assert events[1]["items"][0]["prefix"] == "10.0.0.0/8"
    assert events[2]["items"] == [{"instance_id": "i-2", "public_ip": "10.0.0.2", "reason": "reputation"}]
    assert events[3]["kept"][0]["public_ip"] == "10.0.0.1" and "i-2" in events[3]["terminated"]
    assert events[3]["backend_errors"] == {}


def test_should_stop_cancels_before_next_round_and_marks_winner():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"])
    flag = {"stop": False}
    def backend_probe_then_flag():
        b = ScriptedBackend({"10.0.0.1": 150.0, "10.0.0.2": 100.0})
        orig = b.probe
        def probe(cands):
            flag["stop"] = True
            return orig(cands)
        b.probe = probe
        return b
    orch = Orchestrator(_cfg(max_rounds=3), ec2, FakeSsm(), [backend_probe_then_flag()], [], lambda ip: "",
                        INFRA, "xb-t", log=lambda *a: None, should_stop=lambda: flag["stop"])
    rr = orch.run()
    assert len(rr.rounds) == 1 and rr.stop_reason == "cancelled"
    assert rr.winners[0].candidate.public_ip == "10.0.0.2"
    assert ec2.winners == [("i-2", rr.winners[0].composite, 1)]
    assert set(ec2.live) == {"i-2"}


def test_orchestrator_passes_prefix_history_and_agent_flag_to_scoring():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    cfg = _cfg(max_rounds=1, weights={"prefix_history": 0.5, "prefix_min_samples": 1})
    hist = {"10.0.0.0/8": {"samples": 5, "mean_composite": 40.0, "best_composite": 60.0}}
    orch = Orchestrator(cfg, ec2, FakeSsm(), [ScriptedBackend({"10.0.0.1": 50.0, "10.0.0.2": 50.0})], [],
                        lambda ip: "10.0.0.0/8", INFRA, "xb-h", log=lambda *a: None, prefix_history=hist)
    rr = orch.run()
    w = rr.winners[0]
    assert w.instant_composite == 100.0 and w.prefix_history_score == 40.0 and w.composite == 70.0
    assert orch.agent_enabled is False


class ScriptedAgent(ScriptedBackend):
    name = "agent"
    def probe(self, candidates):
        out = super().probe(candidates)
        for pr in out.values():
            pr.backend = "agent"
        return out


def test_orchestrator_vetoes_when_agent_enabled_but_agent_returns_nothing():
    ec2 = FakeEc2(["10.0.0.1"])
    cfg = _cfg(max_rounds=1, batch_size=1, weights={"backends": {"reverse": 0.1, "agent": 0.9}})

    class SilentAgent(ProbeBackend):
        name = "agent"
        def probe(self, candidates): return {}

    orch = Orchestrator(cfg, ec2, FakeSsm(), [ScriptedBackend({"10.0.0.1": 50.0}), SilentAgent()], [],
                        lambda ip: "", INFRA, "xb-a", log=lambda *a: None)
    rr = orch.run()
    assert orch.agent_enabled is True
    assert rr.winners == [] and rr.rounds[0].scored[0].veto_reason == "agent_unavailable"


def test_winner_loses_probe_group_and_listener_and_probe_group_is_deleted():
    class ProbeEc2(FakeEc2):
        def __init__(self, ips):
            super().__init__(ips); self.detached, self.deleted_groups = [], []
        def detach_probe_group(self, iid, gid): self.detached.append((iid, gid))
        def delete_security_group(self, gid, attempts=6): self.deleted_groups.append(gid); return True

    class ProbeSsm(FakeSsm):
        def __init__(self): super().__init__(); self.scripts = []
        def run_script(self, iid, script, timeout_s=60): self.scripts.append((iid, script)); return "Success", ""

    ec2, ssm = ProbeEc2(["10.0.0.1", "10.0.0.2"]), ProbeSsm()
    infra = Infra("subnet-1", "sg-1", "prof", "ami-1", probe_security_group_id="sg-probe",
                  user_data="#!/bin/bash\n# crossborder_listener\n")
    orch = Orchestrator(_cfg(max_rounds=1), ec2, ssm, [ScriptedBackend({"10.0.0.1": 50.0, "10.0.0.2": 200.0})], [],
                        lambda ip: "", infra, "xb-probe", log=lambda *a: None)
    rr = orch.run()
    winner = rr.winners[0].candidate.instance_id
    assert ec2.detached == [(winner, "sg-probe")]                     # 只对保留实例摘组
    assert any(iid == winner and "crossborder_listener" in s for iid, s in ssm.scripts)  # 停掉临时监听
    assert ec2.deleted_groups == ["sg-probe"]                        # 拨测组随运行结束删除


def test_no_probe_group_means_no_detach_or_delete():
    class ProbeEc2(FakeEc2):
        def __init__(self, ips):
            super().__init__(ips); self.detached, self.deleted_groups = [], []
        def detach_probe_group(self, iid, gid): self.detached.append((iid, gid))
        def delete_security_group(self, gid, attempts=6): self.deleted_groups.append(gid); return True
    ec2 = ProbeEc2(["10.0.0.1"])
    Orchestrator(_cfg(max_rounds=1, batch_size=1), ec2, FakeSsm(), [ScriptedBackend({})], [], lambda ip: "", INFRA,
                 "xb-plain", log=lambda *a: None).run()
    assert ec2.detached == [] and ec2.deleted_groups == []
