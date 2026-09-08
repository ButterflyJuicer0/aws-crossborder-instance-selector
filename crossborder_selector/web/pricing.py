"""机型候选表、估算价与成本公式。价格为估算值，实际以账单为准。"""

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


def estimate(instance_type: str, batch_size: int, max_rounds: int) -> dict:
    cost = batch_size * max_rounds * (hourly_for(instance_type) + IPV4_HOURLY_USD) * ROUND_MINUTES / 60
    return {"estimated_cost_usd": round(cost, 3), "estimated_minutes": max_rounds * ROUND_MINUTES,
            "note": "估算值，实际以账单为准"}
