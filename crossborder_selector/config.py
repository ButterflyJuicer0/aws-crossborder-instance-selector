"""配置模型：默认值 + YAML 深合并 + CLI 覆盖 + 校验。"""
import copy
from dataclasses import dataclass, field

import yaml

KNOWN_BACKENDS = ("reverse", "globalping", "ripeatlas", "itdog")
ISPS = ("telecom", "unicom", "mobile")

DEFAULTS = {
    "region": "ap-east-1",
    "instance_type": "t3.nano",
    "image_id": "",
    "subnet_id": "",
    "security_group_id": "",
    "instance_profile_name": "",
    "batch_size": 10,
    "max_rounds": 3,
    "keep_top_k": 1,
    "target_score": 90.0,
    "min_backends": 1,
    "ssm_online_timeout_s": 180,
    "protect": False,
    "backends": {
        "reverse": {
            "enabled": True,
            "ping_count": 10,
            "tcping_count": 5,
            "tcping_port": 443,
            "timeout_s": 120,
            "targets": {
                "telecom": ["114.114.114.114", "www.189.cn"],
                "unicom": ["123.123.123.123", "www.10010.com"],
                "mobile": ["221.130.33.52", "www.10086.cn"],
            },
        },
        "globalping": {"enabled": True, "locations": ["HK", "TW"], "limit_per_location": 3,
                       "packets": 4, "timeout_s": 60},
        "ripeatlas": {"enabled": False, "api_key": "", "probe_count": 10, "packets": 4,
                      "timeout_s": 120},
        "itdog": {
            "enabled": False,
            "timeout_s": 30,
            # node_id -> isp，默认北京/上海三网 + 深圳电信/移动
            "nodes": {"1310": "telecom", "1273": "unicom", "1250": "mobile",
                      "1227": "telecom", "1254": "unicom", "1249": "mobile",
                      "1169": "telecom", "1290": "mobile"},
        },
    },
    "weights": {
        "backends": {"reverse": 0.5, "globalping": 0.2, "ripeatlas": 0.15, "itdog": 0.15},
        "isps": {"telecom": 0.34, "unicom": 0.33, "mobile": 0.33},
        "lat_good_ms": 60,
        "lat_bad_ms": 300,
    },
    "reputation": {
        "dnsbl_zones": ["zen.spamhaus.org", "b.barracudacentral.org"],
        "badlist_url": ("https://raw.githubusercontent.com/mitchellkrogza/"
                        "nginx-ultimate-bad-bot-blocker/master/_generator_lists/bad-ip-addresses.list"),
        "abuseipdb_api_key": "",
    },
    "output_dir": "./out",
    "history_file": "./history/prefix_stats.json",
}


@dataclass
class Config:
    region: str
    instance_type: str
    image_id: str
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    batch_size: int
    max_rounds: int
    keep_top_k: int
    target_score: float
    min_backends: int
    ssm_online_timeout_s: int
    protect: bool
    backends: dict
    weights: dict
    reputation: dict
    output_dir: str
    history_file: str


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _validate(d: dict) -> None:
    if d["keep_top_k"] < 1:
        raise ValueError("keep_top_k must be >= 1")
    if d["batch_size"] < 1:
        raise ValueError("batch_size must be >= 1")
    if d["max_rounds"] < 1:
        raise ValueError("max_rounds must be >= 1")
    w = d["weights"]
    if set(w["isps"]) != set(ISPS):
        raise ValueError(f"weights.isps must have exactly keys {ISPS}")
    if not set(w["backends"]) <= set(KNOWN_BACKENDS):
        raise ValueError(f"weights.backends keys must be subset of {KNOWN_BACKENDS}")
    if not w["lat_good_ms"] < w["lat_bad_ms"]:
        raise ValueError("lat_good_ms must be < lat_bad_ms")
    if not set(d["backends"]) <= set(KNOWN_BACKENDS):
        raise ValueError(f"backends keys must be subset of {KNOWN_BACKENDS}")
    targets = d["backends"]["reverse"]["targets"]
    for isp in ISPS:
        if not targets.get(isp):
            raise ValueError(f"backends.reverse.targets.{isp} needs at least one target")


def load_config(path=None, overrides=None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    if path:
        with open(path) as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})
    overrides = dict(overrides or {})
    enable = overrides.pop("enable_backends", []) or []
    disable = overrides.pop("disable_backends", []) or []
    data = _deep_merge(data, overrides)
    for name in list(enable) + list(disable):
        if name not in KNOWN_BACKENDS:
            raise ValueError(f"unknown backend {name}; known: {KNOWN_BACKENDS}")
    for name in enable:
        data["backends"][name]["enabled"] = True
    for name in disable:
        data["backends"][name]["enabled"] = False
    _validate(data)
    return Config(**data)
