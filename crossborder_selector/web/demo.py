"""演示模式：不触碰 AWS 的模拟编排器 + 假 client 集合，用于本地试跑与录屏。"""
import random
import time

from crossborder_selector.aws.ec2 import RUN_TAG, WINNER_TAG
from crossborder_selector.aws.infra import Infra
from crossborder_selector.config import ISPS
from crossborder_selector.models import (Candidate, CandidateScore, IspProbe, ProbeResult,
                                         ReputationResult, RoundResult, RunResult, SourceResult)
from crossborder_selector.orchestrator import utc_now_iso
from crossborder_selector.scoring import rank
from crossborder_selector.web import pricing

# IP 段轮换：与真实 HK region 常见 prefix 对齐，prefix 固定便于报告展示
_BASES = ["18.162", "43.198", "16.162"]
_PREFIXES = {"18.162": "18.162.0.0/16", "43.198": "43.198.0.0/15", "16.162": "16.162.0.0/16"}


class DemoOrchestrator:
    """与 Orchestrator 同构造签名（多一个 step_delay）；run() 产出完整 RunResult，可喂给 write_reports。"""

    def __init__(self, cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id,
                 clock=utc_now_iso, log=print, on_event=None, should_stop=None, step_delay=0.4):
        self.cfg, self.run_id = cfg, run_id
        self.now, self.log = clock, log
        self.on_event = on_event or (lambda e: None)
        self.should_stop = should_stop or (lambda: False)
        self.step_delay = step_delay
        self.rnd = random.Random(run_id)  # 以 run_id 播种，保证同一 run_id 可重复

    def _emit(self, type_, **fields):
        self.on_event({"type": type_, "ts": self.now(), **fields})

    def _sleep(self):
        if self.step_delay:
            time.sleep(self.step_delay)

    def _candidate(self, rno, idx):
        base = _BASES[idx % len(_BASES)]
        ip = f"{base}.{self.rnd.randint(1, 254)}.{self.rnd.randint(1, 254)}"
        return Candidate(f"i-demo{rno}{idx:02d}", ip, _PREFIXES[base], rno, self.now())

    def _score_candidate(self, c):
        composite = round(self.rnd.uniform(55, 97), 1)
        # rtt 由 composite 反推：composite 越高 rtt 越低，再叠加 ±8ms 抖动
        rtt = 300 - (300 - 60) * composite / 100
        probes = [IspProbe(isp, 4, 4, round(rtt + self.rnd.uniform(-8, 8), 1), method="ping") for isp in ISPS]
        return CandidateScore(c, None, [ProbeResult("reverse", probes)],
                              {isp: composite for isp in ISPS}, {"reverse": composite},
                              composite, True, "")

    def run(self) -> RunResult:
        started, rounds, incumbents, stop = self.now(), [], [], "max_rounds"
        for rno in range(1, self.cfg.max_rounds + 1):
            if self.should_stop():  # 每轮开始前检查取消
                stop = "cancelled"
                break
            self._emit("round_started", round=rno, batch_size=self.cfg.batch_size)
            self.log(f"[demo round {rno}] launching {self.cfg.batch_size} candidates")
            self._sleep()

            cands = [self._candidate(rno, i) for i in range(self.cfg.batch_size)]
            self._emit("candidates", round=rno,
                       items=[{"instance_id": c.instance_id, "public_ip": c.public_ip, "prefix": c.prefix}
                              for c in cands])
            self._sleep()

            vetoed, scored = [], []
            for i, c in enumerate(cands):
                if rno == 1 and i == 0:  # 第 1 轮固定一个信誉否决
                    rep = ReputationResult(c.public_ip, [SourceResult("dnsbl", True, "zen.spamhaus.org")], 50.0)
                    vetoed.append(CandidateScore(c, rep, [], {}, {}, 0.0, False, "reputation"))
                elif rno == 1 and i == 1 and self.cfg.batch_size - 2 >= self.cfg.keep_top_k:  # 第 1 轮固定一个三网不可达否决；批量过小时跳过，保证仍能填满 keep_top_k
                    pr = ProbeResult("reverse", [IspProbe(isp, 4, 0, None, method="ping") for isp in ISPS])
                    vetoed.append(CandidateScore(c, None, [pr], {}, {}, 0.0, False, "reverse_unreachable"))
                else:
                    scored.append(self._score_candidate(c))
            self._emit("vetoed", round=rno,
                       items=[{"instance_id": v.candidate.instance_id, "public_ip": v.candidate.public_ip,
                               "reason": v.veto_reason} for v in vetoed])
            self._sleep()

            pool = rank(list(incumbents) + scored)  # 合并在位者后重排
            kept = [s for s in pool if s.qualified][: self.cfg.keep_top_k]
            keep_ids = {s.candidate.instance_id for s in kept}
            terminated = [v.candidate.instance_id for v in vetoed] + \
                         [s.candidate.instance_id for s in pool if s.candidate.instance_id not in keep_ids]
            self.log(f"[demo round {rno}] kept={[s.candidate.public_ip for s in kept]}")
            self._emit("round_done", round=rno,
                       kept=[{"instance_id": s.candidate.instance_id, "public_ip": s.candidate.public_ip,
                              "composite": s.composite} for s in kept],
                       terminated=list(terminated), backend_errors={})
            rounds.append(RoundResult(rno, cands, vetoed, scored, kept, terminated, {}))
            incumbents = kept

            if self.should_stop():  # 本轮结束后再查一次：尊重取消，保留在位 winner
                stop = "cancelled"
                break
            if incumbents and incumbents[0].composite >= self.cfg.target_score:
                stop = "target_score_reached"
                break
        return RunResult(self.run_id, self.cfg.region, rounds, incumbents, started, self.now(), stop)


class _DemoEc2:
    """满足 Api.env / Api.options / Ec2Manager 的全部调用；写操作只记录不生效。"""

    def __init__(self):
        self.calls = []

    def describe_vpcs(self, **kw):
        return {"Vpcs": [{"VpcId": "vpc-demo", "IsDefault": True}]}

    def describe_subnets(self, **kw):
        return {"Subnets": [{"SubnetId": "subnet-demo-a", "AvailabilityZone": "ap-east-1a"},
                            {"SubnetId": "subnet-demo-b", "AvailabilityZone": "ap-east-1b"}]}

    def describe_instances(self, **kw):
        names = {f["Name"] for f in kw.get("Filters", [])}
        if f"tag:{WINNER_TAG}" in names:  # 无在位 winner
            return {"Reservations": []}
        if f"tag:{RUN_TAG}" in names:  # 无本 run 遗留实例
            return {"Reservations": []}
        # Api.env 的运行中实例统计：固定 3 台
        return {"Reservations": [{"Instances": [
            {"InstanceId": f"i-demo-run{i}", "State": {"Name": "running"}} for i in range(3)]}]}

    def get_service_quota(self, **kw):
        return {"Quota": {"Value": 64.0}}

    def describe_instance_type_offerings(self, **kw):
        return {"InstanceTypeOfferings": [{"InstanceType": i["type"]} for i in pricing.INSTANCE_CATALOG]}

    def modify_instance_attribute(self, **kw):
        self.calls.append(("modify_instance_attribute", kw))
        return {}

    def terminate_instances(self, **kw):
        self.calls.append(("terminate_instances", kw))
        return {}

    def create_tags(self, **kw):
        self.calls.append(("create_tags", kw))
        return {}

    def delete_tags(self, **kw):
        self.calls.append(("delete_tags", kw))
        return {}


class _DemoQuotas:
    def get_service_quota(self, **kw):
        return {"Quota": {"Value": 64.0}}


class _DemoSts:
    def get_caller_identity(self, **kw):
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/demo"}


def demo_factory(cfg) -> dict:
    """返回一整套假 client；已含 sts 与 service-quotas，避免被 Api 包装层触发真实 boto3。"""
    return {"ec2": _DemoEc2(), "iam": object(), "ssm": object(),
            "sts": _DemoSts(), "service-quotas": _DemoQuotas()}


def demo_ensure_infra(ec2, iam, ssm, cfg) -> Infra:
    return Infra("subnet-demo", "sg-demo", "crossborder-selector-ssm", "ami-demo")


def install_demo(run_manager, step_delay=0.4):
    """把 RunManager 及其 runs 模块的 AWS 入口替换为 demo 版本；返回 restore() 撤销。"""
    import crossborder_selector.web.runs as runs_mod
    saved = (runs_mod.ensure_infra, runs_mod.build_backends, runs_mod.load_ip_ranges,
             run_manager.orchestrator_factory)
    runs_mod.ensure_infra = demo_ensure_infra
    runs_mod.build_backends = lambda cfg, ssm: []
    runs_mod.load_ip_ranges = lambda cache_path=None: []
    run_manager.orchestrator_factory = lambda *a, **kw: DemoOrchestrator(*a, step_delay=step_delay, **kw)

    def restore():
        (runs_mod.ensure_infra, runs_mod.build_backends, runs_mod.load_ip_ranges,
         run_manager.orchestrator_factory) = saved

    return restore
