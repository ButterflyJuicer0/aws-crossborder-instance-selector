"""全部数据模型。只放数据与派生属性，不放逻辑。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Candidate:
    instance_id: str
    public_ip: str
    prefix: str = ""          # 来自 ip-ranges.json，如 "18.162.0.0/16"
    round: int = 0
    launched_at: str = ""     # ISO8601 UTC
    ssm_online: bool = True   # orchestrator 在 wait_online 后设置


@dataclass
class SourceResult:
    source: str
    listed: bool
    detail: str = ""


@dataclass
class ReputationResult:
    address: str
    results: list
    score: float

    @property
    def any_listed(self) -> bool:
        return any(r.listed for r in self.results)


@dataclass
class IspProbe:
    """一个 backend 对一个 ISP（或 HK/TW 地区）的一组原始样本。"""
    isp: str
    sent: int
    received: int
    median_rtt_ms: Optional[float] = None
    target: str = ""
    method: str = ""          # ping / tcp

    @property
    def loss(self) -> float:
        if self.sent <= 0:
            return 1.0
        return (self.sent - self.received) / self.sent


@dataclass
class ProbeResult:
    backend: str
    probes: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.probes)


@dataclass
class CandidateScore:
    candidate: Candidate
    reputation: Optional[ReputationResult]
    probe_results: list
    isp_scores: dict
    backend_scores: dict
    composite: float
    qualified: bool
    veto_reason: str = ""


@dataclass
class RoundResult:
    round: int
    launched: list            # list[Candidate]
    vetoed: list              # list[CandidateScore]
    scored: list              # list[CandidateScore]
    kept: list                # list[CandidateScore]，本轮结束后的全局在位者
    terminated: list          # list[str] instance ids
    backend_errors: dict      # backend -> error text


@dataclass
class RunResult:
    run_id: str
    region: str
    rounds: list              # list[RoundResult]
    winners: list             # list[CandidateScore]
    started_at: str
    finished_at: str
    stop_reason: str          # target_score_reached / max_rounds / error
