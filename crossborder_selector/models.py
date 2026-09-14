"""全部数据模型。只放数据与派生属性，不放逻辑。"""
from dataclasses import dataclass, field
from typing import Optional
import math


@dataclass
class Candidate:
    instance_id: str
    public_ip: str
    prefix: str = ""          # 来自 ip-ranges.json，如 "18.162.0.0/16"
    round: int = 0
    launched_at: str = ""     # ISO8601 UTC
    ssm_online: bool = True   # orchestrator 在 wait_online 后设置
    image_id: str = ""
    root_volume: dict = field(default_factory=dict)
    instance_type: str = ""


@dataclass
class SourceResult:
    source: str
    listed: bool
    detail: str = ""
    error: str = ""

    @property
    def status(self):
        if self.listed:
            return "listed"
        return "unknown" if self.error or self.detail.startswith("error:") else "clear"


@dataclass
class ReputationResult:
    address: str
    results: list
    score: Optional[float]

    @property
    def any_listed(self) -> bool:
        return any(r.listed for r in self.results)

    @property
    def status(self):
        if self.any_listed:
            return "listed"
        return "clear" if self.results and all(r.status == "clear" for r in self.results) else "unknown"


@dataclass
class IspProbe:
    """一个 backend 对一个 ISP（或 HK/TW 地区）的一组原始样本。"""
    isp: str
    sent: int
    received: int
    median_rtt_ms: Optional[float] = None  # 兼容旧字段名；当前探测器提供平均时延。
    target: str = ""
    method: str = ""          # ping / tcp
    p95_rtt_ms: Optional[float] = None     # 逐包/逐次样本的 P95；探测源不提供逐包数据时为 None
    jitter_ms: Optional[float] = None      # 相邻样本绝对差均值；同上

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
    warning: str = ""         # 部分探针/agent 失败但仍有可用样本时的说明，不影响 ok

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.probes) and all(
            p.sent > 0 and 0 <= p.received <= p.sent and
            (p.received == 0 or (p.median_rtt_ms is not None
                                and math.isfinite(p.median_rtt_ms) and p.median_rtt_ms >= 0))
            for p in self.probes)


@dataclass
class CandidateScore:
    candidate: Candidate
    reputation: Optional[ReputationResult]
    probe_results: list
    isp_scores: dict
    backend_scores: dict
    composite: float          # 最终用于排序的分：本次测量分与 prefix 历史分的加权
    qualified: bool
    veto_reason: str = ""
    instant_composite: float = 0.0               # 仅本次测量的综合分
    prefix_history_score: Optional[float] = None  # 参与融合的 prefix 历史均分；未融合时为 None


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
    stop_reason: str          # target_score_reached / max_rounds / no_qualified / launch_failed / cancelled
