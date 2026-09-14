"""配置模型：默认值 + YAML 深合并 + CLI 覆盖 + 校验。"""
import copy
import math
import re
from dataclasses import dataclass

import yaml

KNOWN_BACKENDS = ("reverse", "globalping", "ripeatlas", "itdog")
ISPS = ("telecom", "unicom", "mobile")


def enabled_backends(backends):
    return [name for name, config in backends.items()
            if config.get("enabled") and
            (name != "ripeatlas" or (config.get("api_key") or "").strip())]

DEFAULTS = {
    "region": "ap-east-1",
    "instance_type": "t3.nano",
    "instance_groups": [],
    "image_id": "",
    "image_preset": "",
    "root_volume_size_gib": None,
    "root_volume_type": "gp3",
    "root_volume_encrypted": True,
    "instance_overrides": [],
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
            # 目标可写 host 或 host:port；纯 IP 是三网 DNS 服务器（走 53），域名走默认 tcping_port
            "targets": {
                "telecom": ["114.114.114.114:53", "www.189.cn"],
                "unicom": ["123.123.123.123:53", "www.10010.com"],
                "mobile": ["221.130.33.52:53", "www.10086.cn"],
            },
        },
        "globalping": {"enabled": True, "locations": ["HK", "TW"], "limit_per_location": 2,
                       "packets": 4, "timeout_s": 60, "api_token": ""},
        "ripeatlas": {"enabled": False, "api_key": "", "probe_count": 10, "packets": 4,
                      "timeout_s": 120},
        "itdog": {
            "enabled": False,
            "timeout_s": 30,
            # node_id -> isp；节点位置和运营商映射需要按服务数据维护。
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
        "require_badlist": True,
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
    root_volume_size_gib: int | None
    root_volume_type: str
    root_volume_encrypted: bool
    instance_overrides: list
    instance_groups: list
    image_preset: str


def launch_groups(cfg):
    """An empty group list keeps the legacy single-type configuration working."""
    return cfg.instance_groups or [{"instance_type": cfg.instance_type, "count": cfg.batch_size}]


def launch_specs(cfg):
    from dataclasses import asdict
    defaults = asdict(cfg)
    types = [g["instance_type"] for g in launch_groups(cfg) for _ in range(g["count"])]
    specs = []
    for index, name in enumerate(types):
        override = cfg.instance_overrides[index] if index < len(cfg.instance_overrides) else {}
        specs.append({**image_override(defaults, override), "instance_type": name})
    return specs


def image_override(defaults, override):
    spec = {**defaults, **override}
    if override.get("image_id"):
        spec["image_preset"] = ""
    elif override.get("image_preset"):
        spec["image_id"] = ""
    return spec


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _validate(d: dict) -> None:
    def number(value, name, low, high=None, integer=False):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < low
                or (high is not None and value > high)
                or (integer and not isinstance(value, int))):
            raise ValueError(f"{name} must be {'an integer' if integer else 'finite'} in "
                             f"[{low}, {high if high is not None else '∞'}]")

    def instance_type(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", value):
            raise ValueError("instance_type must be an EC2 instance type")
    groups = d["instance_groups"]
    if not isinstance(groups, list) or len(groups) > 50:
        raise ValueError("instance_groups must be a list with at most 50 rows")
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"instance_type", "count"}:
            raise ValueError("instance_groups rows require instance_type and count")
        instance_type(group["instance_type"])
        number(group["count"], "instance_groups.count", 1, 50, integer=True)
    if groups:
        d["batch_size"] = sum(g["count"] for g in groups)
        d["instance_type"] = groups[0]["instance_type"]
    for key, maximum in (("keep_top_k", 50), ("batch_size", 50), ("max_rounds", 10),
                         ("min_backends", len(KNOWN_BACKENDS)), ("ssm_online_timeout_s", None)):
        number(d[key], key, 1, maximum, integer=True)
    number(d["target_score"], "target_score", 0, 100)
    if d["keep_top_k"] > d["batch_size"] * d["max_rounds"]:
        raise ValueError("最终保留数量不能超过每轮启动数量 × 最多轮次")
    if not isinstance(d["region"], str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-\d+", d["region"]):
        raise ValueError("region must be an AWS region code, for example ap-east-1")
    instance_type(d["instance_type"])
    def storage(spec):
        if not isinstance(spec["image_id"], str):
            raise ValueError("image_id must be a string")
        if spec["image_preset"] not in ("", "al2023", "ubuntu2404", "ubuntu2204"):
            raise ValueError("image_preset must be al2023, ubuntu2404, ubuntu2204 or empty")
        if spec["image_id"] and spec["image_preset"]:
            raise ValueError("choose image_id or image_preset, not both")
        if spec["root_volume_type"] not in ("gp3", "gp2", "standard"):
            raise ValueError("root_volume_type must be gp3, gp2 or standard")
        if spec["root_volume_size_gib"] is not None:
            number(spec["root_volume_size_gib"], "root_volume_size_gib", 1,
                   {"gp3": 65536, "gp2": 16384, "standard": 1024}[spec["root_volume_type"]], integer=True)
        if not isinstance(spec["root_volume_encrypted"], bool):
            raise ValueError("root_volume_encrypted must be true or false")
    storage(d)
    if not isinstance(d["reputation"]["badlist_url"], str) or not d["reputation"]["badlist_url"].startswith("https://"):
        raise ValueError("reputation.badlist_url must be an HTTPS raw IP list URL")
    if not isinstance(d["reputation"]["require_badlist"], bool):
        raise ValueError("reputation.require_badlist must be true or false")
    overrides = d["instance_overrides"]
    if not isinstance(overrides, list) or len(overrides) > d["batch_size"]:
        raise ValueError("instance_overrides must be a list no longer than batch_size")
    keys = {"image_id", "image_preset", "root_volume_size_gib", "root_volume_type", "root_volume_encrypted"}
    for spec in overrides:
        if not isinstance(spec, dict) or not set(spec) <= keys:
            raise ValueError("instance_overrides only accepts image and root volume settings")
        storage(image_override(d, spec))
    if not isinstance(d["protect"], bool):
        raise ValueError("protect must be true or false")
    w = d["weights"]
    if set(w["isps"]) != set(ISPS):
        raise ValueError(f"weights.isps must have exactly keys {ISPS}")
    if not set(w["backends"]) <= set(KNOWN_BACKENDS):
        raise ValueError(f"weights.backends keys must be subset of {KNOWN_BACKENDS}")
    for group in ("isps", "backends"):
        for key, value in w[group].items():
            number(value, f"weights.{group}.{key}", 0)
        if sum(w[group].values()) <= 0:
            raise ValueError(f"weights.{group} must have a positive total")
    number(w["lat_good_ms"], "weights.lat_good_ms", 0)
    number(w["lat_bad_ms"], "weights.lat_bad_ms", 0)
    if not w["lat_good_ms"] < w["lat_bad_ms"]:
        raise ValueError("lat_good_ms must be < lat_bad_ms")
    if not set(d["backends"]) <= set(KNOWN_BACKENDS):
        raise ValueError(f"backends keys must be subset of {KNOWN_BACKENDS}")
    for name, config in d["backends"].items():
        if not isinstance(config["enabled"], bool):
            raise ValueError(f"backends.{name}.enabled must be true or false")
        number(config["timeout_s"], f"backends.{name}.timeout_s", 1)
        for key in ("ping_count", "tcping_count", "packets", "probe_count", "limit_per_location"):
            if key in config:
                number(config[key], f"backends.{name}.{key}", 1, integer=True)
    active = enabled_backends(d["backends"])
    if len(active) < d["min_backends"]:
        raise ValueError("min_backends exceeds usable enabled probe sources (RIPE Atlas needs an API key)")
    if not any(w["backends"].get(name, 0) > 0 for name in active):
        raise ValueError("at least one enabled probe source must have a positive weight")
    number(d["backends"]["reverse"]["tcping_port"], "tcping_port", 1, 65535, integer=True)
    targets = d["backends"]["reverse"]["targets"]
    for isp in ISPS:
        if not isinstance(targets.get(isp), list) or not targets[isp]:
            raise ValueError(f"backends.reverse.targets.{isp} needs at least one target")
        for target in targets[isp]:
            if not isinstance(target, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?", target):
                raise ValueError(f"invalid probe target: {target!r}; use hostname or IPv4[:port]")
            if ":" in target:
                number(int(target.rsplit(":", 1)[1]), "target port", 1, 65535, integer=True)
    if set(targets) != set(ISPS):
        raise ValueError(f"reverse targets must have exactly keys {ISPS}")


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
    try:
        _validate(data)
        return Config(**data)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid configuration structure: {exc}") from exc
