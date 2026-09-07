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


def test_offline_ssm_marks_candidate_and_min_backends_veto():
    ec2 = FakeEc2(["10.0.0.1", "10.0.0.2"])
    class Offline(ProbeBackend):
        name = "reverse"
        def probe(self, cands):
            return {c.public_ip: (ProbeResult("reverse", [], "ssm offline") if not c.ssm_online
                                  else ProbeResult("reverse", [IspProbe("telecom", 4, 4, 30.0)])) for c in cands}
    orch = Orchestrator(_cfg(max_rounds=1), ec2, FakeSsm(offline=["i-1"]), [Offline()], [], lambda ip: "", INFRA, "xb-t", log=lambda *a: None)
    rr = orch.run()
    scored = {s.candidate.instance_id: s for s in rr.rounds[0].scored}
    assert scored["i-1"].veto_reason == "min_backends" and scored["i-2"].qualified
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
