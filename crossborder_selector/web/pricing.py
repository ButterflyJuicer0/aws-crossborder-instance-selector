"""机型候选表、估算价与成本公式。价格为估算值，实际以账单为准。"""
import math
from crossborder_selector.config import DEFAULTS, enabled_backends

IPV4_HOURLY_USD = 0.005
ROUND_MINUTES = 8
DEFAULT_HOURLY_USD = 0.02

INSTANCE_CATALOG = [
    {"type": "t3.nano", "vcpu": 2, "memory_gib": 0.5, "arch": "x86_64", "hourly_usd": 0.0066},
    {"type": "t3.micro", "vcpu": 2, "memory_gib": 1.0, "arch": "x86_64", "hourly_usd": 0.0132},
    {"type": "t3.small", "vcpu": 2, "memory_gib": 2.0, "arch": "x86_64", "hourly_usd": 0.0264},
    {"type": "t3.medium", "vcpu": 2, "memory_gib": 4.0, "arch": "x86_64", "hourly_usd": 0.0528},
    {"type": "t4g.nano", "vcpu": 2, "memory_gib": 0.5, "arch": "arm64", "hourly_usd": 0.0053},
    {"type": "t4g.micro", "vcpu": 2, "memory_gib": 1.0, "arch": "arm64", "hourly_usd": 0.0106},
    {"type": "t4g.small", "vcpu": 2, "memory_gib": 2.0, "arch": "arm64", "hourly_usd": 0.0212},
    {"type": "m6g.medium", "vcpu": 1, "memory_gib": 4.0, "arch": "arm64", "hourly_usd": 0.0495},
    {"type": "c6g.medium", "vcpu": 1, "memory_gib": 2.0, "arch": "arm64", "hourly_usd": 0.0435},
]

REGIONS = [
    {"code": "ap-east-1", "name": "亚太（香港）"},
    {"code": "ap-northeast-1", "name": "亚太（东京）"},
    {"code": "ap-southeast-1", "name": "亚太（新加坡）"},
    {"code": "ap-northeast-2", "name": "亚太（首尔）"},
    {"code": "ap-northeast-3", "name": "亚太（大阪）"},
    {"code": "us-west-2", "name": "美国西部（俄勒冈）"},
    {"code": "us-west-1", "name": "美国西部（加利福尼亚）"},
]


def hourly_for(instance_type: str) -> float:
    for item in INSTANCE_CATALOG:
        if item["type"] == instance_type:
            return item["hourly_usd"]
    return DEFAULT_HOURLY_USD


def estimate(instance_type: str, batch_size: int, max_rounds: int, *, region="ap-east-1",
             backends=None, keep_top_k=1, ssm_online_timeout_s=180, instance_groups=None) -> dict:
    backends = backends or DEFAULTS["backends"]
    active = enabled_backends(backends)
    budgets = []
    for name in active:
        b = backends[name]
        if name == "reverse":
            budgets.append(math.ceil(batch_size / 8) * (b["timeout_s"] + 30))
        elif name == "globalping":
            budgets.append(batch_size * (b["timeout_s"] + 40))
        elif name == "ripeatlas":
            budgets.append(batch_size * (b["timeout_s"] + 40 + 20 * b["probe_count"]))
        else:
            budgets.append(2 * (10 + b["timeout_s"]) + 15 + b["timeout_s"])
    # Planning assumptions, not execution deadlines: instance startup, serial reputation checks,
    # SSM registration, and parallel sources (each source may itself process candidates serially).
    low_per_round = 5 + batch_size * 2 / 60
    high_per_round = 10 + batch_size * 40 / 60 + ssm_online_timeout_s / 60 + max(budgets, default=0) / 60
    minutes = [math.ceil(low_per_round * max_rounds), math.ceil(high_per_round * max_rounds)]
    groups = instance_groups or [{"instance_type": instance_type, "count": batch_size}]
    rates = [hourly_for(g["instance_type"]) + IPV4_HOURLY_USD for g in groups for _ in range(g["count"])]
    known = region == "ap-east-1" and all(any(i["type"] == g["instance_type"] for i in INSTANCE_CATALOG) for g in groups)
    hourly_rounds = sum(rates) * max_rounds + max(rates) * keep_top_k * max(0, max_rounds - 1)
    reference = [round(hourly_rounds * duration / 60, 3)
                 for duration in (low_per_round, high_per_round)]
    return {"estimated_cost_usd": reference[1] if known else None,
            "estimated_cost_range_usd": reference if known else None,
            "estimated_minutes": minutes[1], "estimated_minutes_range": minutes,
            "price_reference_region": "ap-east-1", "price_source": "项目内静态参考表，未实时核价",
            "note": ("EC2 + 公网 IPv4 参考估算，含跨轮保留实例；未含 EBS 根卷、流量和最终保留实例的后续费用。"
                     "跨轮保留按清单最高单价估算；时长为规划范围，重试或服务异常可能延长。"
                     + ("" if known else "当前区域或机型没有已核实的单价，费用暂不估算。"))}
