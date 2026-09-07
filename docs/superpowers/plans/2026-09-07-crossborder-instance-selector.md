# AWS 跨境优选实例选择器 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一条命令在指定 AWS Region 多轮批量启动候选 EC2，从大陆视角拨测其自动分配的公网 IPv4，自动保留全局 Top-K 实例、终止其余，并输出 JSON/Markdown/CSV 报告。

**Architecture:** Python 包 `crossborder_selector`。`orchestrator` 只编排，云操作收敛在 `aws/`（EC2 / IAM+SG 基础设施 / SSM / ip-ranges），拨测收敛在 `probes/`（`ProbeBackend` 接口，四个实现），打分在 `scoring`，输出在 `report`。所有可终止资源按 `crossborder-run-id` 标签管理；winner 选出后即移除该标签。

**Tech Stack:** Python 3.11+，boto3，requests，dnspython，PyYAML，websockets（仅 itdog），pytest，moto。

**Spec:** `docs/superpowers/specs/2026-09-07-crossborder-instance-selector-design.md`

## Global Constraints

- 仓库根：`/Users/jinhaoz/Documents/TLI/Solution/aws-crossborder-instance-selector`；所有命令在该目录执行。
- 包名 `crossborder_selector`；测试目录 `tests/`；`pytest.ini` 设 `testpaths = tests`。
- 不做真实网络与真实云测试。AWS 用 moto；SSM 与四个探测 backend 用注入的假 client/transport。
- 标签键固定：`crossborder-run-id`、`crossborder-round`、`crossborder-winner`、`crossborder-score`、`crossborder-selected-at`、`crossborder-managed`。
- 基础设施名称固定：SG `crossborder-selector-sg`，IAM role `crossborder-selector-ssm-role`，instance profile `crossborder-selector-ssm`。
- SSM 探测输出标记：一行以 `CROSSBORDER_JSON:` 开头的 JSON 数组。
- run-id 格式：`xb-<UTC YYYYMMDDTHHMMSSZ>-<4 位 hex>`。
- 三网键固定：`telecom`、`unicom`、`mobile`。backend 名固定：`reverse`、`globalping`、`ripeatlas`、`itdog`。
- 每个任务结束提交一次 git；提交信息用英文 conventional commits。
- 术语：注释与文档用中文，标识符英文。避免非包容性词汇（使用 allowlist/denylist）。

---

## 文件结构

| 路径 | 职责 |
|---|---|
| `crossborder_selector/models.py` | 全部 dataclass：Candidate、IspProbe、ProbeResult、SourceResult、ReputationResult、CandidateScore、RoundResult、RunResult |
| `crossborder_selector/config.py` | 默认值、深合并、校验、`Config` dataclass、`load_config` |
| `crossborder_selector/reputation/{base,dnsbl,badlist,abuseipdb}.py` | 从 clean-ip-selection 移植的信誉源 |
| `crossborder_selector/aws/ipranges.py` | 下载/缓存 ip-ranges.json，IP → prefix |
| `crossborder_selector/aws/infra.py` | 默认 VPC/子网、SG、IAM role + instance profile、AMI 解析 |
| `crossborder_selector/aws/ec2.py` | `Ec2Manager`：launch/wait/public_ips/terminate/标签操作/cleanup |
| `crossborder_selector/aws/ssm.py` | `SsmRunner`：等待 online、执行脚本并取回输出 |
| `crossborder_selector/probes/base.py` | `ProbeBackend` 抽象与 `run_backends` 并行执行器 |
| `crossborder_selector/probes/reverse.py` | 三网反向探测脚本生成、SSM 执行、输出解析 |
| `crossborder_selector/probes/globalping.py` | Globalping HK/TW |
| `crossborder_selector/probes/ripeatlas.py` | RIPE Atlas 大陆探针 |
| `crossborder_selector/probes/itdog.py` | itdog 非官方 WebSocket 协议 |
| `crossborder_selector/scoring.py` | 子分、合成、硬否决、排序 |
| `crossborder_selector/orchestrator.py` | 锦标赛循环、winner 标签、异常清理 |
| `crossborder_selector/report.py` | JSON/Markdown/CSV 与 prefix 历史 |
| `crossborder_selector/cli.py` | `select` / `cleanup` / `report` 子命令 |
| `scripts/find_best_instance.sh` | 一条命令入口 |
| `config.example.yaml`、`README.md`、`MANUAL.md` | 配置样例与文档 |

---

### Task 1: 仓库骨架与数据模型

**Files:**
- Create: `requirements.txt`、`pytest.ini`、`.gitignore`、`crossborder_selector/__init__.py`、`crossborder_selector/models.py`、`tests/__init__.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `Candidate(instance_id, public_ip, prefix="", round=0, launched_at="", ssm_online=True)`；`IspProbe(isp, sent, received, median_rtt_ms=None, target="", method="")` 含 `loss` 属性；`ProbeResult(backend, probes=[], error="")` 含 `ok` 属性；`SourceResult`、`ReputationResult`（含 `any_listed`）；`CandidateScore`；`RoundResult`；`RunResult`。后续所有任务都依赖这些名字。

- [ ] **Step 1: 建骨架文件**

```bash
cd /Users/jinhaoz/Documents/TLI/Solution/aws-crossborder-instance-selector
mkdir -p crossborder_selector/aws crossborder_selector/probes crossborder_selector/reputation tests scripts
touch crossborder_selector/__init__.py crossborder_selector/aws/__init__.py \
      crossborder_selector/probes/__init__.py crossborder_selector/reputation/__init__.py tests/__init__.py
cat > requirements.txt <<'EOF'
boto3>=1.34
requests>=2.31
dnspython>=2.4
PyYAML>=6.0
websockets>=12.0
pytest>=7.4
moto[ec2,iam,ssm]>=5.0
EOF
cat > pytest.ini <<'EOF'
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
EOF
cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
out/
history/
config.yaml
.pytest_cache/
EOF
python3 -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
```

- [ ] **Step 2: 写失败测试**

`tests/test_models.py`：

```python
from crossborder_selector.models import (
    Candidate, IspProbe, ProbeResult, SourceResult, ReputationResult,
)


def test_isp_probe_loss():
    assert IspProbe("telecom", 10, 8).loss == 0.2
    assert IspProbe("telecom", 0, 0).loss == 1.0


def test_probe_result_ok_requires_probes_and_no_error():
    assert ProbeResult("reverse", [IspProbe("telecom", 1, 1, 10.0)]).ok is True
    assert ProbeResult("reverse", []).ok is False
    assert ProbeResult("reverse", [IspProbe("telecom", 1, 1)], error="boom").ok is False


def test_reputation_any_listed():
    rr = ReputationResult("1.2.3.4", [SourceResult("a", False), SourceResult("b", True)], 50.0)
    assert rr.any_listed is True


def test_candidate_defaults():
    c = Candidate("i-1", "1.2.3.4")
    assert c.prefix == "" and c.round == 0 and c.ssm_online is True
```

- [ ] **Step 3: 运行确认失败**

Run: `pytest tests/test_models.py -v`
Expected: FAIL，`ModuleNotFoundError: crossborder_selector.models`

- [ ] **Step 4: 实现 models.py**

```python
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
        return 1.0 - self.received / self.sent


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
```

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_models.py -v`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat: repo skeleton and data models"
```

---

### Task 2: 配置加载与校验

**Files:**
- Create: `crossborder_selector/config.py`、`config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass（字段见下）；`load_config(path: str | None, overrides: dict | None) -> Config`；`DEFAULTS` dict；常量 `KNOWN_BACKENDS = ("reverse","globalping","ripeatlas","itdog")`、`ISPS = ("telecom","unicom","mobile")`。`overrides` 键与 Config 字段同名，另支持 `enable_backends: list[str]`、`disable_backends: list[str]`。

- [ ] **Step 1: 写失败测试**

`tests/test_config.py`：

```python
import pytest
import yaml

from crossborder_selector.config import load_config, DEFAULTS, KNOWN_BACKENDS, ISPS


def test_defaults_without_file():
    cfg = load_config(None)
    assert cfg.region == "ap-east-1"
    assert cfg.batch_size == 10 and cfg.max_rounds == 3 and cfg.keep_top_k == 1
    assert cfg.backends["reverse"]["enabled"] is True
    assert cfg.backends["ripeatlas"]["enabled"] is False
    assert set(cfg.weights["isps"]) == set(ISPS)


def test_file_deep_merges_over_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"batch_size": 4, "backends": {"reverse": {"ping_count": 3}}}))
    cfg = load_config(str(p))
    assert cfg.batch_size == 4
    assert cfg.backends["reverse"]["ping_count"] == 3
    assert cfg.backends["reverse"]["enabled"] is True  # 未覆盖的键保留默认


def test_overrides_and_backend_toggles():
    cfg = load_config(None, {"region": "ap-northeast-1", "enable_backends": ["itdog"],
                             "disable_backends": ["globalping"]})
    assert cfg.region == "ap-northeast-1"
    assert cfg.backends["itdog"]["enabled"] is True
    assert cfg.backends["globalping"]["enabled"] is False


@pytest.mark.parametrize("bad", [
    {"weights": {"isps": {"telecom": 1.0, "unicom": 1.0, "mobile": 1.0, "extra": 1.0}}},
    {"weights": {"lat_good_ms": 300, "lat_bad_ms": 60}},
    {"keep_top_k": 0},
    {"batch_size": 0},
    {"max_rounds": 0},
    {"backends": {"reverse": {"targets": {"telecom": [], "unicom": ["x"], "mobile": ["y"]}}}},
    {"weights": {"backends": {"bogus": 1.0}}},
])
def test_validation_errors(bad):
    with pytest.raises(ValueError):
        load_config(None, bad)


def test_unknown_enable_backend_rejected():
    with pytest.raises(ValueError):
        load_config(None, {"enable_backends": ["nope"]})


def test_known_backends_constant():
    assert KNOWN_BACKENDS == ("reverse", "globalping", "ripeatlas", "itdog")
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 config.py**

```python
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
```

- [ ] **Step 4: 写 config.example.yaml**

内容为 DEFAULTS 的 YAML 形式并加中文注释（每个顶层键一行注释说明用途；`ripeatlas.api_key`、`abuseipdb_api_key` 注释"留空则跳过"；`itdog.enabled` 注释"非官方接口，默认关闭"）。用 `python -c "import yaml,crossborder_selector.config as c;print(yaml.safe_dump(c.DEFAULTS, allow_unicode=True, sort_keys=False))"` 生成基础文本再手工加注释。

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_config.py -v`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat: config defaults, deep merge, validation"
```

---

### Task 3: 移植 IP 信誉模块

**Files:**
- Create: `crossborder_selector/reputation/base.py`、`dnsbl.py`、`badlist.py`、`abuseipdb.py`
- Test: `tests/test_reputation.py`

**Interfaces:**
- Produces: `ReputationSource`（抽象，属性 `name`、`weight`，方法 `check(address) -> SourceResult`）；`score_reputation(address, sources) -> ReputationResult`；`build_sources(config: dict) -> list[ReputationSource]`（读 `dnsbl_zones`、`badlist_url`、`abuseipdb_api_key`）。

- [ ] **Step 1: 写失败测试**

`tests/test_reputation.py`：

```python
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource, score_reputation
from crossborder_selector.reputation.dnsbl import DnsblSource, reverse_ip
from crossborder_selector.reputation.badlist import BadListSource
from crossborder_selector.reputation.abuseipdb import AbuseIpdbSource, build_sources


class FakeSource(ReputationSource):
    def __init__(self, name, weight, listed):
        self.name = name; self.weight = weight; self._listed = listed
    def check(self, address):
        return SourceResult(self.name, self._listed, "x" if self._listed else "")


class BoomSource(ReputationSource):
    name = "boom"; weight = 50.0
    def check(self, address):
        raise RuntimeError("dns timeout")


def test_clean_ip_scores_100():
    rr = score_reputation("1.2.3.4", [FakeSource("a", 40, False), FakeSource("b", 60, False)])
    assert rr.score == 100.0 and rr.any_listed is False


def test_listed_ip_loses_weight_and_floors_at_zero():
    assert score_reputation("1.2.3.4", [FakeSource("a", 40, True), FakeSource("b", 60, False)]).score == 60.0
    assert score_reputation("1.2.3.4", [FakeSource("a", 70, True), FakeSource("b", 60, True)]).score == 0.0


def test_source_error_does_not_deduct():
    rr = score_reputation("1.2.3.4", [BoomSource()])
    assert rr.score == 100.0 and rr.results[0].detail.startswith("error:")


def test_dnsbl():
    assert reverse_ip("1.2.3.4") == "4.3.2.1"
    class R:
        def resolve(self, q, t):
            if q == "4.3.2.1.zen.example.org":
                return ["127.0.0.2"]
            raise Exception("NXDOMAIN")
    s = DnsblSource(zones=["zen.example.org", "bl.example.org"], resolver=R())
    assert s.check("1.2.3.4").listed is True
    assert s.check("5.6.7.8").listed is False


def test_badlist_hit_miss_and_single_fetch():
    calls = {"n": 0}
    def fetcher(u):
        calls["n"] += 1
        return "# c\n45.78.235.240\n\n"
    s = BadListSource(url="http://x", fetcher=fetcher)
    assert s.check("45.78.235.240").listed is True
    assert s.check("1.1.1.1").listed is False
    assert calls["n"] == 1


def test_abuseipdb_threshold():
    hi = AbuseIpdbSource(api_key="k", caller=lambda k, a: {"abuseConfidenceScore": 90})
    lo = AbuseIpdbSource(api_key="k", caller=lambda k, a: {"abuseConfidenceScore": 0})
    assert hi.check("1.2.3.4").listed is True and lo.check("1.2.3.4").listed is False


def test_build_sources_key_toggle():
    base = {"dnsbl_zones": ["z"], "badlist_url": "http://x", "abuseipdb_api_key": ""}
    assert "abuseipdb" not in {s.name for s in build_sources(base)}
    assert "abuseipdb" in {s.name for s in build_sources({**base, "abuseipdb_api_key": "k"})}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_reputation.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现四个文件**

`reputation/base.py`：

```python
from abc import ABC, abstractmethod
from crossborder_selector.models import SourceResult, ReputationResult


class ReputationSource(ABC):
    name: str = "source"
    weight: float = 50.0

    @abstractmethod
    def check(self, address: str) -> SourceResult: ...


def score_reputation(address: str, sources) -> ReputationResult:
    results, score = [], 100.0
    for s in sources:
        try:
            r = s.check(address)
        except Exception as e:  # 单源失败不影响整体，也不扣分
            r = SourceResult(s.name, False, f"error:{e}")
        results.append(r)
        if r.listed:
            score -= s.weight
    return ReputationResult(address=address, results=results, score=max(0.0, score))
```

`reputation/dnsbl.py`：

```python
import dns.resolver
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource


def reverse_ip(address: str) -> str:
    return ".".join(reversed(address.split(".")))


class DnsblSource(ReputationSource):
    name = "dnsbl"

    def __init__(self, zones, weight: float = 50.0, resolver=None):
        self.zones = zones
        self.weight = weight
        self._resolver = resolver or dns.resolver.Resolver()

    def check(self, address: str) -> SourceResult:
        rev, hits = reverse_ip(address), []
        for zone in self.zones:
            try:
                if self._resolver.resolve(f"{rev}.{zone}", "A"):
                    hits.append(zone)
            except Exception:
                continue  # 未命中或解析失败均视为该 zone 不在名单
        return SourceResult(self.name, bool(hits), ",".join(hits))
```

`reputation/badlist.py`：

```python
import requests
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource


def _http_get(url: str) -> str:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


class BadListSource(ReputationSource):
    name = "badlist"

    def __init__(self, url: str, weight: float = 80.0, fetcher=None):
        self.url, self.weight = url, weight
        self._fetcher = fetcher or _http_get
        self._ips = None

    def _load(self):
        if self._ips is None:
            text = self._fetcher(self.url)
            self._ips = {ln.strip() for ln in text.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")}
        return self._ips

    def check(self, address: str) -> SourceResult:
        listed = address in self._load()
        return SourceResult(self.name, listed, self.url if listed else "")
```

`reputation/abuseipdb.py`：

```python
import requests
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource
from crossborder_selector.reputation.dnsbl import DnsblSource
from crossborder_selector.reputation.badlist import BadListSource


def _default_caller(api_key: str, address: str) -> dict:
    resp = requests.get("https://api.abuseipdb.com/api/v2/check",
                        headers={"Key": api_key, "Accept": "application/json"},
                        params={"ipAddress": address, "maxAgeInDays": 90}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", {})


class AbuseIpdbSource(ReputationSource):
    name = "abuseipdb"

    def __init__(self, api_key: str, threshold: int = 25, weight: float = 70.0, caller=None):
        self.api_key, self.threshold, self.weight = api_key, threshold, weight
        self._caller = caller or _default_caller

    def check(self, address: str) -> SourceResult:
        score = int(self._caller(self.api_key, address).get("abuseConfidenceScore", 0))
        return SourceResult(self.name, score >= self.threshold, f"confidence={score}")


def build_sources(config: dict):
    sources = [DnsblSource(zones=config["dnsbl_zones"]), BadListSource(url=config["badlist_url"])]
    key = (config.get("abuseipdb_api_key") or "").strip()
    if key:
        sources.append(AbuseIpdbSource(api_key=key))
    return sources
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_reputation.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: port IP reputation sources (dnsbl, badlist, abuseipdb)"
```

---

### Task 4: ip-ranges prefix 归类

**Files:**
- Create: `crossborder_selector/aws/ipranges.py`
- Test: `tests/test_ipranges.py`

**Interfaces:**
- Produces: `load_ip_ranges(fetcher=None, cache_path=None) -> list[dict]`（返回 `prefixes` 数组，每项含 `ip_prefix`、`region`、`service`）；`prefix_for(ip: str, prefixes: list, region: str) -> str`；`PrefixLookup(prefixes, region)` 可调用对象 `__call__(ip) -> str`。下载失败返回空列表，不抛异常。

- [ ] **Step 1: 写失败测试**

`tests/test_ipranges.py`：

```python
import json
from crossborder_selector.aws.ipranges import load_ip_ranges, prefix_for, PrefixLookup

RANGES = {"prefixes": [
    {"ip_prefix": "18.162.0.0/16", "region": "ap-east-1", "service": "EC2"},
    {"ip_prefix": "18.160.0.0/13", "region": "ap-east-1", "service": "AMAZON"},
    {"ip_prefix": "43.198.0.0/15", "region": "ap-east-1", "service": "AMAZON"},
    {"ip_prefix": "3.0.0.0/8", "region": "GLOBAL", "service": "AMAZON"},
]}


def test_load_uses_fetcher_and_returns_prefixes():
    got = load_ip_ranges(fetcher=lambda url: json.dumps(RANGES))
    assert len(got) == 4


def test_load_failure_returns_empty():
    def boom(url):
        raise OSError("offline")
    assert load_ip_ranges(fetcher=boom) == []


def test_prefix_prefers_ec2_then_most_specific():
    p = RANGES["prefixes"]
    assert prefix_for("18.162.1.1", p, "ap-east-1") == "18.162.0.0/16"
    assert prefix_for("43.198.9.9", p, "ap-east-1") == "43.198.0.0/15"
    assert prefix_for("3.4.5.6", p, "ap-east-1") == "3.0.0.0/8"
    assert prefix_for("8.8.8.8", p, "ap-east-1") == ""


def test_cache_written_and_reused(tmp_path):
    calls = {"n": 0}
    def fetcher(url):
        calls["n"] += 1
        return json.dumps(RANGES)
    cache = tmp_path / "ipr.json"
    load_ip_ranges(fetcher=fetcher, cache_path=str(cache))
    load_ip_ranges(fetcher=fetcher, cache_path=str(cache))
    assert calls["n"] == 1 and cache.exists()


def test_lookup_callable():
    lk = PrefixLookup(RANGES["prefixes"], "ap-east-1")
    assert lk("18.162.0.5") == "18.162.0.0/16"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_ipranges.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 ipranges.py**

```python
"""AWS 公布的 ip-ranges.json：下载、缓存、IP → prefix。"""
import ipaddress
import json
import os
import urllib.request

IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode()


def load_ip_ranges(fetcher=None, cache_path=None) -> list:
    """返回 prefixes 列表；有缓存文件先读缓存；任何失败返回 []。"""
    fetcher = fetcher or _http_get
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f).get("prefixes", [])
        except (OSError, ValueError):
            pass
    try:
        text = fetcher(IP_RANGES_URL)
        data = json.loads(text)
    except Exception:
        return []
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w") as f:
                f.write(text)
        except OSError:
            pass
    return data.get("prefixes", [])


def prefix_for(ip: str, prefixes: list, region: str) -> str:
    """优先 EC2 服务、同 region；其次最长前缀。找不到返回空串。"""
    addr = ipaddress.ip_address(ip)
    best, best_key = "", None
    for p in prefixes:
        try:
            net = ipaddress.ip_network(p["ip_prefix"])
        except (KeyError, ValueError):
            continue
        if addr not in net:
            continue
        key = (p.get("service") == "EC2", p.get("region") == region, net.prefixlen)
        if best_key is None or key > best_key:
            best, best_key = p["ip_prefix"], key
    return best


class PrefixLookup:
    def __init__(self, prefixes: list, region: str):
        self.prefixes, self.region = prefixes, region

    def __call__(self, ip: str) -> str:
        return prefix_for(ip, self.prefixes, self.region)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_ipranges.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: ip-ranges prefix lookup with cache"
```

---

### Task 5: 基础设施幂等创建（SG / IAM / AMI / 子网）

**Files:**
- Create: `crossborder_selector/aws/infra.py`
- Test: `tests/test_infra.py`

**Interfaces:**
- Produces: `Infra(subnet_id, security_group_id, instance_profile_name, image_id)` dataclass；`ensure_infra(ec2, iam, ssm, cfg: Config) -> Infra`；`delete_infra(ec2, iam) -> None`；`arch_for_instance_type(t) -> "x86_64"|"arm64"`；`resolve_ami(ssm, instance_type) -> str`。常量 `SG_NAME`、`ROLE_NAME`、`PROFILE_NAME`、`MANAGED_TAG`。默认 VPC 缺失抛 `RuntimeError`，消息含 "no default VPC"。

- [ ] **Step 1: 写失败测试**

`tests/test_infra.py`：

```python
import boto3
import pytest
from moto import mock_aws

from crossborder_selector.aws.infra import (
    ensure_infra, delete_infra, arch_for_instance_type, resolve_ami,
    SG_NAME, ROLE_NAME, PROFILE_NAME, AMI_PARAMS,
)
from crossborder_selector.config import load_config

REGION = "us-east-1"


def _clients():
    return (boto3.client("ec2", region_name=REGION), boto3.client("iam", region_name=REGION),
            boto3.client("ssm", region_name=REGION))


def _seed_ami(ssm, ec2, arch="x86_64"):
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    ssm.put_parameter(Name=AMI_PARAMS[arch], Value=ami, Type="String")
    return ami


def test_arch_detection():
    assert arch_for_instance_type("t3.nano") == "x86_64"
    assert arch_for_instance_type("t4g.nano") == "arm64"
    assert arch_for_instance_type("c6gn.large") == "arm64"
    assert arch_for_instance_type("g4dn.xlarge") == "x86_64"


@mock_aws
def test_ensure_infra_creates_then_reuses():
    ec2, iam, ssm = _clients()
    ami = _seed_ami(ssm, ec2)
    cfg = load_config(None, {"region": REGION})
    a = ensure_infra(ec2, iam, ssm, cfg)
    b = ensure_infra(ec2, iam, ssm, cfg)
    assert a == b
    assert a.image_id == ami and a.instance_profile_name == PROFILE_NAME
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]
    assert len(sgs) == 1 and sgs[0]["IpPermissions"] == []
    assert {t["Key"]: t["Value"] for t in sgs[0]["Tags"]}["crossborder-managed"] == "true"
    roles = iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]
    assert any(p["PolicyName"] == "AmazonSSMManagedInstanceCore" for p in roles)


@mock_aws
def test_ensure_infra_honours_explicit_ids():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    vpc = ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.9.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="mine", Description="d", VpcId=vpc)["GroupId"]
    cfg = load_config(None, {"region": REGION, "subnet_id": subnet, "security_group_id": sg,
                             "image_id": "ami-custom", "instance_profile_name": "my-profile"})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    assert infra.subnet_id == subnet and infra.security_group_id == sg
    assert infra.image_id == "ami-custom" and infra.instance_profile_name == "my-profile"


def test_missing_default_vpc_raises():
    class NoDefaultVpc:
        class meta:
            region_name = REGION
        def describe_vpcs(self, Filters):
            return {"Vpcs": []}
    from crossborder_selector.aws.infra import find_default_subnet
    with pytest.raises(RuntimeError, match="no default VPC"):
        find_default_subnet(NoDefaultVpc())


@mock_aws
def test_delete_infra_removes_managed_resources():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    ensure_infra(ec2, iam, ssm, load_config(None, {"region": REGION}))
    delete_infra(ec2, iam)
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"] == []
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)


@mock_aws
def test_resolve_ami_arm():
    ec2, iam, ssm = _clients()
    ami = _seed_ami(ssm, ec2, "arm64")
    assert resolve_ami(ssm, "t4g.nano") == ami
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_infra.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 infra.py**

```python
"""跨 run 复用的基础设施：默认子网、无入站 SG、SSM instance profile、AL2023 AMI。全部幂等。"""
import json
import re
from dataclasses import dataclass

from botocore.exceptions import ClientError

SG_NAME = "crossborder-selector-sg"
ROLE_NAME = "crossborder-selector-ssm-role"
PROFILE_NAME = "crossborder-selector-ssm"
MANAGED_TAG = {"Key": "crossborder-managed", "Value": "true"}
SSM_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
AMI_PARAMS = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
}
_TRUST = json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]})


@dataclass(frozen=True)
class Infra:
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    image_id: str


def arch_for_instance_type(instance_type: str) -> str:
    family = instance_type.split(".")[0]
    return "arm64" if re.match(r"^[a-z]+\d+[a-z]*g[a-z]*$", family) else "x86_64"


def resolve_ami(ssm, instance_type: str) -> str:
    r = ssm.get_parameters(Names=[AMI_PARAMS[arch_for_instance_type(instance_type)]])
    if not r["Parameters"]:
        raise RuntimeError("cannot resolve AL2023 AMI via SSM public parameter")
    return r["Parameters"][0]["Value"]


def find_default_subnet(ec2, subnet_id: str = ""):
    """返回 (vpc_id, subnet_id)。给了 subnet_id 就只查它的 VPC。"""
    if subnet_id:
        s = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        return s["VpcId"], subnet_id
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError(f"no default VPC in region {ec2.meta.region_name}; "
                           "set subnet_id and security_group_id in config")
    vpc = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"default VPC {vpc} has no subnet; set subnet_id in config")
    return vpc, sorted(subnets, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]


def ensure_security_group(ec2, vpc_id: str) -> str:
    """按名字查找；不存在则创建。无任何入站规则。"""
    found = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [SG_NAME]}, {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    return ec2.create_security_group(
        GroupName=SG_NAME, Description="crossborder selector probe egress-only", VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": [MANAGED_TAG]}])["GroupId"]


def ensure_instance_profile(iam) -> str:
    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        return PROFILE_NAME
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    try:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=_TRUST,
                        Description="crossborder selector SSM role", Tags=[MANAGED_TAG])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=SSM_POLICY_ARN)
    iam.create_instance_profile(InstanceProfileName=PROFILE_NAME, Tags=[MANAGED_TAG])
    iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    return PROFILE_NAME


def ensure_infra(ec2, iam, ssm, cfg) -> Infra:
    vpc_id, subnet_id = find_default_subnet(ec2, cfg.subnet_id)
    sg = cfg.security_group_id or ensure_security_group(ec2, vpc_id)
    profile = cfg.instance_profile_name or ensure_instance_profile(iam)
    image = cfg.image_id or resolve_ami(ssm, cfg.instance_type)
    return Infra(subnet_id, sg, profile, image)


def delete_infra(ec2, iam) -> None:
    """删除工具自建的 SG 与 IAM 资源。调用方负责先确认没有 winner 依赖。"""
    for sg in ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]:
        ec2.delete_security_group(GroupId=sg["GroupId"])
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    except ClientError:
        pass
    for fn, kw in ((iam.delete_instance_profile, {"InstanceProfileName": PROFILE_NAME}),
                   (iam.detach_role_policy, {"RoleName": ROLE_NAME, "PolicyArn": SSM_POLICY_ARN}),
                   (iam.delete_role, {"RoleName": ROLE_NAME})):
        try:
            fn(**kw)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_infra.py -v`
Expected: 6 passed（若 moto 对 `Tags` 参数报错，去掉 `create_role`/`create_instance_profile` 的 `Tags=` 再重跑，并在文件顶部注释说明）

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: idempotent infra (egress-only SG, SSM instance profile, AL2023 AMI)"
```

---

### Task 6: Ec2Manager

**Files:**
- Create: `crossborder_selector/aws/ec2.py`
- Test: `tests/test_ec2.py`

**Interfaces:**
- Produces: `Ec2Manager(client, sleeper=time.sleep)` 方法：`launch(n, run_id, round_no, infra: Infra, instance_type) -> list[str]`；`wait_running(ids)`；`public_ips(ids) -> dict[str,str]`；`terminate(ids)`；`list_run_instances(run_id) -> list[str]`（排除 terminated/shutting-down）；`mark_winner(instance_id, run_id, score: float, round_no: int, now_iso: str)`；`protect(instance_id)`；`has_winners() -> bool`。常量 `RUN_TAG`、`ROUND_TAG`、`WINNER_TAG`、`SCORE_TAG`、`SELECTED_TAG`。

- [ ] **Step 1: 写失败测试**

`tests/test_ec2.py`：

```python
import boto3
from moto import mock_aws

from crossborder_selector.aws.ec2 import Ec2Manager, RUN_TAG, ROUND_TAG, WINNER_TAG, SCORE_TAG
from crossborder_selector.aws.infra import Infra, ensure_instance_profile

REGION = "us-east-1"


def _setup():
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="g", Description="d", VpcId=vpc)["GroupId"]
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    profile = ensure_instance_profile(iam)
    return ec2, Infra(subnet, sg, profile, ami)


def _tags(ec2, iid):
    r = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return {t["Key"]: t["Value"] for t in r.get("Tags", [])}


@mock_aws
def test_launch_tags_and_public_ips():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    ids = m.launch(3, "xb-run1", 2, infra, "t3.nano")
    assert len(ids) == 3
    m.wait_running(ids)
    ips = m.public_ips(ids)
    assert set(ips) == set(ids) and all(ips.values())
    assert _tags(ec2, ids[0]) == {RUN_TAG: "xb-run1", ROUND_TAG: "2"}


@mock_aws
def test_list_and_terminate_only_this_run():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    a = m.launch(2, "xb-a", 1, infra, "t3.nano")
    b = m.launch(1, "xb-b", 1, infra, "t3.nano")
    assert set(m.list_run_instances("xb-a")) == set(a)
    m.terminate(a)
    assert m.list_run_instances("xb-a") == []
    assert set(m.list_run_instances("xb-b")) == set(b)
    m.terminate([])  # 空列表不报错


@mock_aws
def test_mark_winner_swaps_tags_and_has_winners():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    (iid,) = m.launch(1, "xb-w", 1, infra, "t3.nano")
    assert m.has_winners() is False
    m.mark_winner(iid, "xb-w", 93.456, 2, "2026-09-07T00:00:00Z")
    t = _tags(ec2, iid)
    assert RUN_TAG not in t
    assert t[WINNER_TAG] == "true" and t[SCORE_TAG] == "93.5" and t["crossborder-round"] == "2"
    assert m.list_run_instances("xb-w") == [] and m.has_winners() is True


@mock_aws
def test_protect_sets_both_attributes():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    (iid,) = m.launch(1, "xb-p", 1, infra, "t3.nano")
    m.protect(iid)
    assert ec2.describe_instance_attribute(InstanceId=iid, Attribute="disableApiTermination")["DisableApiTermination"]["Value"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_ec2.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 ec2.py**

```python
"""EC2 候选机生命周期。所有查询与删除都按 run-id 标签过滤。"""
import time

from botocore.exceptions import ClientError

RUN_TAG = "crossborder-run-id"
ROUND_TAG = "crossborder-round"
WINNER_TAG = "crossborder-winner"
SCORE_TAG = "crossborder-score"
SELECTED_TAG = "crossborder-selected-at"
_LIVE_STATES = ["pending", "running", "stopping", "stopped"]


class Ec2Manager:
    def __init__(self, client, sleeper=time.sleep):
        self.ec2 = client
        self._sleep = sleeper

    def launch(self, n, run_id, round_no, infra, instance_type) -> list:
        """启动 n 台；IAM profile 刚建好时 RunInstances 会报 InvalidParameterValue，重试最多 6 次。"""
        tags = [{"Key": RUN_TAG, "Value": run_id}, {"Key": ROUND_TAG, "Value": str(round_no)}]
        last = None
        for _ in range(6):
            try:
                r = self.ec2.run_instances(
                    ImageId=infra.image_id, InstanceType=instance_type, MinCount=1, MaxCount=n,
                    IamInstanceProfile={"Name": infra.instance_profile_name},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": infra.subnet_id,
                                        "Groups": [infra.security_group_id],
                                        "AssociatePublicIpAddress": True}],
                    TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
                    InstanceInitiatedShutdownBehavior="terminate")
                return [i["InstanceId"] for i in r["Instances"]]
            except ClientError as e:
                last = e
                if e.response["Error"]["Code"] != "InvalidParameterValue":
                    raise
                self._sleep(5)
        raise last

    def wait_running(self, ids):
        if ids:
            self.ec2.get_waiter("instance_running").wait(InstanceIds=list(ids))

    def public_ips(self, ids) -> dict:
        out = {}
        if not ids:
            return out
        for res in self.ec2.describe_instances(InstanceIds=list(ids))["Reservations"]:
            for i in res["Instances"]:
                out[i["InstanceId"]] = i.get("PublicIpAddress", "")
        return out

    def terminate(self, ids):
        if ids:
            self.ec2.terminate_instances(InstanceIds=list(ids))

    def list_run_instances(self, run_id) -> list:
        r = self.ec2.describe_instances(Filters=[
            {"Name": f"tag:{RUN_TAG}", "Values": [run_id]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES}])
        return [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]

    def mark_winner(self, instance_id, run_id, score, round_no, now_iso):
        self.ec2.delete_tags(Resources=[instance_id], Tags=[{"Key": RUN_TAG}, {"Key": ROUND_TAG}])
        self.ec2.create_tags(Resources=[instance_id], Tags=[
            {"Key": WINNER_TAG, "Value": "true"}, {"Key": SCORE_TAG, "Value": f"{score:.1f}"},
            {"Key": ROUND_TAG, "Value": str(round_no)}, {"Key": SELECTED_TAG, "Value": now_iso},
            {"Key": "crossborder-source-run", "Value": run_id}])

    def protect(self, instance_id):
        self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiTermination={"Value": True})
        try:
            self.ec2.modify_instance_attribute(InstanceId=instance_id, DisableApiStop={"Value": True})
        except ClientError:
            pass  # 少数机型/老 API 不支持 stop protection，不致命

    def has_winners(self) -> bool:
        r = self.ec2.describe_instances(Filters=[
            {"Name": f"tag:{WINNER_TAG}", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES}])
        return any(res["Instances"] for res in r["Reservations"])
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_ec2.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Ec2Manager with run-id scoped lifecycle and winner tagging"
```

---

### Task 7: SsmRunner

**Files:**
- Create: `crossborder_selector/aws/ssm.py`
- Test: `tests/test_ssm.py`

**Interfaces:**
- Produces: `SsmRunner(client, sleeper=time.sleep, clock=time.time, poll_s=3)` 方法：`wait_online(instance_ids, timeout_s) -> set[str]`（返回 PingStatus 为 Online 的 id 集合）；`run_script(instance_id, script: str, timeout_s=120) -> tuple[str, str]`，返回 `(status, output)`，status 取 SSM 的 `Success` / `Failed` / `TimedOut` / `Cancelled` / `DeliveryTimedOut`，本地等待超时返回 `("LocalTimeout", "")`。

- [ ] **Step 1: 写失败测试**

`tests/test_ssm.py`：

```python
from crossborder_selector.aws.ssm import SsmRunner


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class FakeSsm:
    def __init__(self, online_after=1, invocation_statuses=None, output="CROSSBORDER_JSON:[]"):
        self.calls = 0
        self.online_after = online_after
        self.statuses = list(invocation_statuses or ["InProgress", "Success"])
        self.output = output
        self.sent = []

    def describe_instance_information(self, Filters):
        self.calls += 1
        ids = Filters[0]["Values"]
        status = "Online" if self.calls >= self.online_after else "ConnectionLost"
        return {"InstanceInformationList": [{"InstanceId": i, "PingStatus": status} for i in ids]}

    def send_command(self, **kw):
        self.sent.append(kw)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, CommandId, InstanceId):
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Status": st, "StandardOutputContent": self.output, "StandardErrorContent": ""}


def test_wait_online_returns_online_set():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(online_after=2), sleeper=clk.sleep, clock=clk, poll_s=3)
    assert r.wait_online(["i-1", "i-2"], timeout_s=30) == {"i-1", "i-2"}


def test_wait_online_times_out_with_partial():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(online_after=999), sleeper=clk.sleep, clock=clk, poll_s=5)
    assert r.wait_online(["i-1"], timeout_s=12) == set()
    assert clk.t >= 12


def test_run_script_polls_until_success():
    clk = FakeClock()
    fake = FakeSsm()
    r = SsmRunner(fake, sleeper=clk.sleep, clock=clk)
    status, out = r.run_script("i-1", "echo hi", timeout_s=60)
    assert status == "Success" and out.startswith("CROSSBORDER_JSON:")
    sent = fake.sent[0]
    assert sent["DocumentName"] == "AWS-RunShellScript" and sent["InstanceIds"] == ["i-1"]
    assert sent["Parameters"]["commands"] == ["echo hi"] and sent["TimeoutSeconds"] == 60


def test_run_script_local_timeout():
    clk = FakeClock()
    r = SsmRunner(FakeSsm(invocation_statuses=["InProgress"]), sleeper=clk.sleep, clock=clk, poll_s=10)
    assert r.run_script("i-1", "sleep 999", timeout_s=25)[0] == "LocalTimeout"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_ssm.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 ssm.py**

```python
"""通过 SSM 在候选机上执行脚本。不需要 SSH、key pair 或入站规则。"""
import time

from botocore.exceptions import ClientError

_TERMINAL = {"Success", "Failed", "TimedOut", "Cancelled", "DeliveryTimedOut", "Undeliverable", "Terminated"}


class SsmRunner:
    def __init__(self, client, sleeper=time.sleep, clock=time.time, poll_s=3):
        self.ssm, self._sleep, self._now, self.poll_s = client, sleeper, clock, poll_s

    def wait_online(self, instance_ids, timeout_s) -> set:
        ids, online, deadline = list(instance_ids), set(), self._now() + timeout_s
        while ids:
            r = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": ids}])
            for info in r.get("InstanceInformationList", []):
                if info.get("PingStatus") == "Online":
                    online.add(info["InstanceId"])
            if online >= set(ids) or self._now() >= deadline:
                break
            self._sleep(self.poll_s)
        return online

    def run_script(self, instance_id, script, timeout_s=120):
        cmd = self.ssm.send_command(
            InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script]}, TimeoutSeconds=timeout_s)["Command"]["CommandId"]
        deadline = self._now() + timeout_s + 30
        while True:
            self._sleep(self.poll_s)
            try:
                inv = self.ssm.get_command_invocation(CommandId=cmd, InstanceId=instance_id)
            except ClientError as e:  # InvocationDoesNotExist：命令尚未下发到实例
                if e.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
                inv = {"Status": "Pending"}
            if inv.get("Status") in _TERMINAL:
                return inv["Status"], (inv.get("StandardOutputContent", "") + inv.get("StandardErrorContent", ""))
            if self._now() >= deadline:
                return "LocalTimeout", ""
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_ssm.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: SsmRunner for wait-online and remote script execution"
```

---

### Task 8: 探测 backend 接口与 reverse backend

**Files:**
- Create: `crossborder_selector/probes/base.py`、`crossborder_selector/probes/reverse.py`
- Test: `tests/test_probe_base.py`、`tests/test_probe_reverse.py`

**Interfaces:**
- Consumes: `SsmRunner.run_script`、`Candidate`、`IspProbe`、`ProbeResult`。
- Produces: `ProbeBackend` 抽象（属性 `name: str`，方法 `probe(candidates: list[Candidate]) -> dict[str, ProbeResult]`，键为 `public_ip`）；`run_backends(backends, candidates, max_workers=4) -> tuple[dict[str, list[ProbeResult]], dict[str, str]]`；`build_script(targets: dict, ping_count, tcping_count, tcping_port) -> str`；`parse_output(text) -> list[IspProbe]`（无标记行抛 `ValueError`）；`ReverseBackend(ssm_runner, backend_cfg: dict)`。

- [ ] **Step 1: 写失败测试（base）**

`tests/test_probe_base.py`：

```python
import pytest
from crossborder_selector.models import Candidate, IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend, run_backends

C = [Candidate("i-1", "1.1.1.1"), Candidate("i-2", "2.2.2.2")]


class Good(ProbeBackend):
    name = "good"
    def probe(self, candidates):
        return {c.public_ip: ProbeResult("good", [IspProbe("telecom", 4, 4, 50.0)]) for c in candidates}


class Boom(ProbeBackend):
    name = "boom"
    def probe(self, candidates):
        raise RuntimeError("api down")


def test_abstract():
    with pytest.raises(TypeError):
        ProbeBackend()


def test_run_backends_collects_and_isolates_errors():
    results, errors = run_backends([Good(), Boom()], C)
    assert set(results) == {"1.1.1.1", "2.2.2.2"}
    by_name = {r.backend: r for r in results["1.1.1.1"]}
    assert by_name["good"].ok is True
    assert by_name["boom"].ok is False and "api down" in by_name["boom"].error
    assert "api down" in errors["boom"] and "good" not in errors
```

- [ ] **Step 2: 写失败测试（reverse）**

`tests/test_probe_reverse.py`：

```python
import json
from crossborder_selector.models import Candidate
from crossborder_selector.probes.reverse import build_script, parse_output, ReverseBackend

TARGETS = {"telecom": ["114.114.114.114"], "unicom": ["123.123.123.123"], "mobile": ["221.130.33.52"]}
CFG = {"enabled": True, "ping_count": 4, "tcping_count": 2, "tcping_port": 443, "timeout_s": 60, "targets": TARGETS}


def test_build_script_mentions_every_target_and_marker():
    s = build_script(TARGETS, 4, 2, 443)
    for t in ("114.114.114.114", "123.123.123.123", "221.130.33.52"):
        assert t in s
    assert "ping -c 4" in s and "/dev/tcp/" in s and "CROSSBORDER_JSON:" in s
    assert "set -e" not in s  # 单目标失败不能中断脚本


def test_parse_output():
    payload = [
        {"isp": "telecom", "target": "114.114.114.114", "method": "ping", "sent": 4, "received": 4, "avg_ms": 45.2},
        {"isp": "mobile", "target": "221.130.33.52:443", "method": "tcp", "sent": 2, "received": 0, "avg_ms": None},
    ]
    probes = parse_output("noise\nCROSSBORDER_JSON:" + json.dumps(payload) + "\n")
    assert len(probes) == 2
    assert probes[0].isp == "telecom" and probes[0].median_rtt_ms == 45.2 and probes[0].loss == 0.0
    assert probes[1].received == 0 and probes[1].median_rtt_ms is None and probes[1].method == "tcp"


def test_parse_output_without_marker_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_output("garbage")


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes, self.calls = outcomes, []
    def run_script(self, instance_id, script, timeout_s=120):
        self.calls.append((instance_id, timeout_s))
        return self.outcomes[instance_id]


def test_reverse_backend_maps_statuses():
    good = "CROSSBORDER_JSON:" + json.dumps([{"isp": "telecom", "target": "x", "method": "ping", "sent": 4, "received": 3, "avg_ms": 80}])
    runner = FakeRunner({"i-ok": ("Success", good), "i-fail": ("Failed", "boom"), "i-bad": ("Success", "no marker")})
    b = ReverseBackend(runner, CFG)
    cands = [Candidate("i-ok", "1.1.1.1"), Candidate("i-fail", "2.2.2.2"),
             Candidate("i-bad", "3.3.3.3"), Candidate("i-off", "4.4.4.4", ssm_online=False)]
    out = b.probe(cands)
    assert out["1.1.1.1"].ok and out["1.1.1.1"].probes[0].received == 3
    assert "Failed" in out["2.2.2.2"].error
    assert "marker" in out["3.3.3.3"].error.lower() or "json" in out["3.3.3.3"].error.lower()
    assert out["4.4.4.4"].error == "ssm offline"
    assert all(t == 60 for _, t in runner.calls) and len(runner.calls) == 3
```

- [ ] **Step 3: 运行确认失败**

Run: `pytest tests/test_probe_base.py tests/test_probe_reverse.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 4: 实现 probes/base.py**

```python
"""探测 backend 接口与并行执行器。"""
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from crossborder_selector.models import ProbeResult


class ProbeBackend(ABC):
    name: str = "backend"

    @abstractmethod
    def probe(self, candidates: list) -> dict:
        """返回 {public_ip: ProbeResult}。允许抛异常，由 run_backends 兜底。"""


def run_backends(backends, candidates, max_workers=4):
    """并行跑所有 backend。返回 ({ip: [ProbeResult...]}, {backend: error})。"""
    results = {c.public_ip: [] for c in candidates}
    errors = {}

    def one(b):
        try:
            return b.name, b.probe(candidates), ""
        except Exception as e:  # 单 backend 失败只记错，不影响其他
            return b.name, {}, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, out, err in pool.map(one, backends):
            if err:
                errors[name] = err
            for c in candidates:
                results[c.public_ip].append(out.get(c.public_ip) or ProbeResult(name, [], err or "no result"))
    return results, errors
```

- [ ] **Step 5: 实现 probes/reverse.py**

```python
"""反向探测：候选机经 SSM 向三网目标 ping + tcping，输出一行 JSON。"""
import json
from concurrent.futures import ThreadPoolExecutor

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

MARKER = "CROSSBORDER_JSON:"

_SCRIPT_HEAD = r'''#!/bin/bash
# 由 crossborder_selector 生成。任何单目标失败都不中断。
out=""; sep=""
emit() { out="$out$sep{\"isp\":\"$1\",\"target\":\"$2\",\"method\":\"$3\",\"sent\":$4,\"received\":$5,\"avg_ms\":${6:-null}}"; sep=","; }
probe_ping() { # isp target count
  local r tx rx avg
  r=$(ping -c "$3" -W 2 -i 0.2 "$2" 2>/dev/null)
  tx=$(echo "$r" | sed -n 's/^\([0-9]*\) packets transmitted.*/\1/p'); [ -z "$tx" ] && tx=$3
  rx=$(echo "$r" | sed -n 's/^[0-9]* packets transmitted, \([0-9]*\) .*received.*/\1/p'); [ -z "$rx" ] && rx=0
  avg=$(echo "$r" | sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p')
  emit "$1" "$2" "ping" "$tx" "$rx" "$avg"
}
probe_tcp() { # isp host port count
  local ok=0 total=0 i t0 t1 avg=""
  for i in $(seq 1 "$4"); do
    t0=$(date +%s%N)
    if timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then
      t1=$(date +%s%N); ok=$((ok+1)); total=$((total+(t1-t0)/1000000))
    fi
  done
  [ "$ok" -gt 0 ] && avg=$((total/ok))
  emit "$1" "$2:$3" "tcp" "$4" "$ok" "$avg"
}
'''


def build_script(targets: dict, ping_count: int, tcping_count: int, tcping_port: int) -> str:
    lines = [_SCRIPT_HEAD]
    for isp, hosts in targets.items():
        for h in hosts:
            lines.append(f'probe_ping "{isp}" "{h}" {int(ping_count)}')
            lines.append(f'probe_tcp "{isp}" "{h}" {int(tcping_port)} {int(tcping_count)}')
    lines.append(f'echo "{MARKER}[$out]"')
    return "\n".join(lines) + "\n"


def parse_output(text: str) -> list:
    for line in text.splitlines():
        if line.startswith(MARKER):
            items = json.loads(line[len(MARKER):])
            return [IspProbe(isp=i["isp"], sent=int(i["sent"]), received=int(i["received"]),
                             median_rtt_ms=(None if i.get("avg_ms") is None else float(i["avg_ms"])),
                             target=i.get("target", ""), method=i.get("method", ""))
                    for i in items]
    raise ValueError("reverse probe output has no CROSSBORDER_JSON marker")


class ReverseBackend(ProbeBackend):
    name = "reverse"

    def __init__(self, ssm_runner, backend_cfg: dict):
        self.ssm = ssm_runner
        self.cfg = backend_cfg
        self.script = build_script(backend_cfg["targets"], backend_cfg["ping_count"],
                                   backend_cfg["tcping_count"], backend_cfg["tcping_port"])

    def _one(self, c):
        if not c.ssm_online:
            return c.public_ip, ProbeResult(self.name, [], "ssm offline")
        status, out = self.ssm.run_script(c.instance_id, self.script, timeout_s=self.cfg["timeout_s"])
        if status != "Success":
            return c.public_ip, ProbeResult(self.name, [], f"ssm status {status}: {out[-200:]}")
        try:
            return c.public_ip, ProbeResult(self.name, parse_output(out))
        except (ValueError, KeyError) as e:
            return c.public_ip, ProbeResult(self.name, [], f"parse error: {e}")

    def probe(self, candidates: list) -> dict:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(self._one, candidates))
```

- [ ] **Step 6: 运行确认通过**

Run: `pytest tests/test_probe_base.py tests/test_probe_reverse.py -v`
Expected: 6 passed

- [ ] **Step 7: 在本机 shell 冒烟脚本语法**

```bash
python -c "from crossborder_selector.probes.reverse import build_script; print(build_script({'telecom':['127.0.0.1'],'unicom':['127.0.0.1'],'mobile':['127.0.0.1']},1,1,22))" > /tmp/rev.sh && bash -n /tmp/rev.sh && echo SYNTAX_OK
```
Expected: `SYNTAX_OK`

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "feat: probe backend interface and SSM-based reverse probe"
```

---

### Task 9: 打分

**Files:**
- Create: `crossborder_selector/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `IspProbe`、`ProbeResult`、`ReputationResult`、`Candidate`、`CandidateScore`、`ISPS`。
- Produces: `latency_factor(rtt, good, bad) -> float`；`probe_score(p: IspProbe, good, bad) -> float`；`isp_scores_for(pr: ProbeResult, good, bad) -> dict[str, float]`；`backend_score(isp_scores: dict, isp_weights: dict) -> float`；`score_candidate(candidate, reputation, probe_results: list[ProbeResult], weights: dict, min_backends: int, reverse_enabled: bool) -> CandidateScore`；`rank(scores) -> list[CandidateScore]`。

- [ ] **Step 1: 写失败测试**

`tests/test_scoring.py`：

```python
import pytest
from crossborder_selector.models import Candidate, IspProbe, ProbeResult, ReputationResult, SourceResult
from crossborder_selector.scoring import (latency_factor, probe_score, isp_scores_for, backend_score,
                                          score_candidate, rank)

W = {"backends": {"reverse": 0.5, "globalping": 0.2, "ripeatlas": 0.15, "itdog": 0.15},
     "isps": {"telecom": 0.34, "unicom": 0.33, "mobile": 0.33}, "lat_good_ms": 60, "lat_bad_ms": 300}
CLEAN = ReputationResult("1.1.1.1", [SourceResult("dnsbl", False)], 100.0)
DIRTY = ReputationResult("1.1.1.1", [SourceResult("dnsbl", True)], 50.0)
C = Candidate("i-1", "1.1.1.1")


def rev(t=50.0, u=50.0, m=50.0, rx=4):
    return ProbeResult("reverse", [IspProbe("telecom", 4, rx, t), IspProbe("unicom", 4, rx, u), IspProbe("mobile", 4, rx, m)])


def test_latency_factor_boundaries():
    assert latency_factor(30, 60, 300) == 1.0
    assert latency_factor(60, 60, 300) == 1.0
    assert latency_factor(180, 60, 300) == pytest.approx(0.5)
    assert latency_factor(300, 60, 300) == 0.0
    assert latency_factor(None, 60, 300) == 0.0


def test_probe_score_combines_loss_and_latency():
    assert probe_score(IspProbe("telecom", 10, 10, 60), 60, 300) == 100.0
    assert probe_score(IspProbe("telecom", 10, 5, 180), 60, 300) == pytest.approx(25.0)


def test_isp_scores_average_multiple_targets():
    pr = ProbeResult("reverse", [IspProbe("telecom", 4, 4, 60), IspProbe("telecom", 4, 4, 300)])
    assert isp_scores_for(pr, 60, 300) == {"telecom": pytest.approx(50.0)}


def test_backend_score_weights_three_isps_else_equal_mean():
    assert backend_score({"telecom": 100, "unicom": 100, "mobile": 0}, W["isps"]) == pytest.approx(67.0)
    assert backend_score({"HK": 80, "TW": 40}, W["isps"]) == pytest.approx(60.0)


def test_veto_reputation():
    s = score_candidate(C, DIRTY, [rev()], W, 1, True)
    assert s.qualified is False and s.veto_reason == "reputation"


def test_veto_reverse_unreachable():
    s = score_candidate(C, CLEAN, [rev(rx=0)], W, 1, True)
    assert s.qualified is False and s.veto_reason == "reverse_unreachable"


def test_veto_min_backends():
    s = score_candidate(C, CLEAN, [ProbeResult("reverse", [], "ssm offline")], W, 1, True)
    assert s.qualified is False and s.veto_reason == "min_backends"


def test_composite_renormalizes_over_valid_backends():
    gp = ProbeResult("globalping", [IspProbe("HK", 4, 4, 60)])
    s = score_candidate(C, CLEAN, [rev(60, 60, 60), gp, ProbeResult("ripeatlas", [], "no key")], W, 1, True)
    assert s.qualified is True
    assert s.composite == pytest.approx(100.0)
    assert set(s.backend_scores) == {"reverse", "globalping"}
    s2 = score_candidate(C, CLEAN, [rev(60, 60, 60), ProbeResult("globalping", [IspProbe("HK", 4, 0, None)])], W, 1, True)
    assert s2.composite == pytest.approx(100 * 0.5 / 0.7)


def test_rank_order():
    a = score_candidate(Candidate("a", "1.1.1.1"), CLEAN, [rev(60, 60, 60)], W, 1, True)
    b = score_candidate(Candidate("b", "2.2.2.2"), CLEAN, [rev(180, 180, 180)], W, 1, True)
    v = score_candidate(Candidate("v", "3.3.3.3"), DIRTY, [rev()], W, 1, True)
    assert [s.candidate.instance_id for s in rank([b, v, a])] == ["a", "b", "v"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 scoring.py**

```python
"""子分 → ISP 分 → backend 分 → composite；硬否决；排序。"""
from statistics import mean

from crossborder_selector.config import ISPS
from crossborder_selector.models import CandidateScore


def latency_factor(rtt, good, bad) -> float:
    if rtt is None or rtt >= bad:
        return 0.0
    if rtt <= good:
        return 1.0
    return (bad - rtt) / (bad - good)


def probe_score(p, good, bad) -> float:
    return 100.0 * (1.0 - p.loss) * latency_factor(p.median_rtt_ms, good, bad)


def isp_scores_for(pr, good, bad) -> dict:
    buckets = {}
    for p in pr.probes:
        buckets.setdefault(p.isp, []).append(probe_score(p, good, bad))
    return {isp: mean(v) for isp, v in buckets.items()}


def backend_score(isp_scores: dict, isp_weights: dict) -> float:
    if not isp_scores:
        return 0.0
    if set(isp_scores) <= set(ISPS):
        total = sum(isp_weights[i] for i in isp_scores)
        return sum(isp_scores[i] * isp_weights[i] for i in isp_scores) / total
    return mean(isp_scores.values())


def score_candidate(candidate, reputation, probe_results, weights, min_backends, reverse_enabled) -> CandidateScore:
    good, bad = weights["lat_good_ms"], weights["lat_bad_ms"]
    valid = [pr for pr in probe_results if pr.ok]
    per_backend_isp = {pr.backend: isp_scores_for(pr, good, bad) for pr in valid}
    backend_scores = {b: backend_score(s, weights["isps"]) for b, s in per_backend_isp.items()}
    bw = {b: weights["backends"].get(b, 0.0) for b in backend_scores}
    total_w = sum(bw.values())
    composite = sum(backend_scores[b] * bw[b] for b in backend_scores) / total_w if total_w else 0.0

    isp_scores = {}
    for isp in set(i for s in per_backend_isp.values() for i in s):
        pairs = [(per_backend_isp[b][isp], bw[b]) for b in per_backend_isp if isp in per_backend_isp[b]]
        tw = sum(w for _, w in pairs)
        isp_scores[isp] = sum(v * w for v, w in pairs) / tw if tw else mean(v for v, _ in pairs)

    veto = ""
    if reputation is not None and reputation.any_listed:
        veto = "reputation"
    elif reverse_enabled:
        rev = next((pr for pr in probe_results if pr.backend == "reverse"), None)
        if rev is not None and rev.ok and all(p.received == 0 for p in rev.probes):
            veto = "reverse_unreachable"
    if not veto and len(valid) < min_backends:
        veto = "min_backends"

    return CandidateScore(candidate=candidate, reputation=reputation, probe_results=probe_results,
                          isp_scores=isp_scores, backend_scores=backend_scores,
                          composite=round(composite, 2), qualified=not veto, veto_reason=veto)


def rank(scores) -> list:
    return sorted(scores, key=lambda s: (s.qualified, s.composite, s.backend_scores.get("reverse", 0.0)),
                  reverse=True)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_scoring.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: scoring with hard vetoes and weight renormalization"
```

---

### Task 10: Orchestrator（多轮锦标赛）

**Files:**
- Create: `crossborder_selector/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `Ec2Manager`（launch/wait_running/public_ips/terminate/mark_winner/protect）、`SsmRunner.wait_online`、`run_backends`、`score_reputation`、`score_candidate`、`rank`、`PrefixLookup`、`Infra`、`Config`。
- Produces: `Orchestrator(cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id, clock=None, log=print)`；`run() -> RunResult`；`utc_now_iso() -> str`。

- [ ] **Step 1: 写失败测试**

`tests/test_orchestrator.py`：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 orchestrator.py**

```python
"""滚动锦标赛：每轮启动候选机 → 信誉预筛 → 拨测 → 与在位者合并保留 Top-K → 终止其余。"""
from datetime import datetime, timezone

from crossborder_selector.models import Candidate, CandidateScore, RoundResult, RunResult
from crossborder_selector.probes.base import run_backends
from crossborder_selector.reputation.base import score_reputation
from crossborder_selector.scoring import score_candidate, rank


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Orchestrator:
    def __init__(self, cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id,
                 clock=utc_now_iso, log=print):
        self.cfg, self.ec2, self.ssm = cfg, ec2, ssm
        self.backends, self.rep_sources = backends, reputation_sources
        self.prefix_lookup, self.infra, self.run_id = prefix_lookup, infra, run_id
        self.now, self.log = clock, log
        self.reverse_enabled = any(b.name == "reverse" for b in backends)

    def run(self) -> RunResult:
        started, rounds, incumbents, stop = self.now(), [], [], "max_rounds"
        launched_all = set()
        try:
            for rno in range(1, self.cfg.max_rounds + 1):
                rr = self._round(rno, incumbents)
                launched_all.update(c.instance_id for c in rr.launched)
                rounds.append(rr)
                incumbents = rr.kept
                if incumbents and incumbents[0].composite >= self.cfg.target_score:
                    stop = "target_score_reached"
                    break
        except Exception:
            keep = {s.candidate.instance_id for s in incumbents}
            self.ec2.terminate(sorted(launched_all - keep))
            raise
        if not incumbents:
            stop = "no_qualified"
        for w in incumbents:
            self.ec2.mark_winner(w.candidate.instance_id, self.run_id, w.composite, w.candidate.round, self.now())
            if self.cfg.protect:
                self.ec2.protect(w.candidate.instance_id)
        return RunResult(self.run_id, self.cfg.region, rounds, incumbents, started, self.now(), stop)

    def _round(self, rno, incumbents) -> RoundResult:
        self.log(f"[round {rno}] launching {self.cfg.batch_size} x {self.cfg.instance_type}")
        ids = self.ec2.launch(self.cfg.batch_size, self.run_id, rno, self.infra, self.cfg.instance_type)
        terminated = []
        try:
            self.ec2.wait_running(ids)
            ips = self.ec2.public_ips(ids)
            cands = [Candidate(i, ips[i], self.prefix_lookup(ips[i]) if ips[i] else "", rno, self.now())
                     for i in ids]

            vetoed, survivors = [], []
            for c in cands:
                rep = score_reputation(c.public_ip, self.rep_sources)
                if rep.any_listed:
                    vetoed.append(CandidateScore(c, rep, [], {}, {}, 0.0, False, "reputation"))
                else:
                    survivors.append((c, rep))
            self.ec2.terminate([v.candidate.instance_id for v in vetoed])
            terminated += [v.candidate.instance_id for v in vetoed]

            online = self.ssm.wait_online([c.instance_id for c, _ in survivors], self.cfg.ssm_online_timeout_s)
            for c, _ in survivors:
                c.ssm_online = c.instance_id in online
            results, errors = run_backends(self.backends, [c for c, _ in survivors])
            scored = [score_candidate(c, rep, results.get(c.public_ip, []), self.cfg.weights,
                                      self.cfg.min_backends, self.reverse_enabled) for c, rep in survivors]

            pool = rank(list(incumbents) + scored)
            kept = [s for s in pool if s.qualified][: self.cfg.keep_top_k]
            keep_ids = {s.candidate.instance_id for s in kept}
            losers = [s.candidate.instance_id for s in pool if s.candidate.instance_id not in keep_ids]
            self.ec2.terminate(losers)
            terminated += losers
            self.log(f"[round {rno}] kept={[s.candidate.public_ip for s in kept]} terminated={len(terminated)}")
            return RoundResult(rno, cands, vetoed, scored, kept, terminated, errors)
        except Exception:
            self.ec2.terminate([i for i in ids if i not in terminated])
            raise
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_orchestrator.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: tournament orchestrator with winner tagging and failure cleanup"
```

---

### Task 11: 报告输出与 prefix 历史

**Files:**
- Create: `crossborder_selector/report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `RunResult`、`CandidateScore`、`Config`。
- Produces: `to_dict(run: RunResult, cfg: Config) -> dict`；`render_markdown(data: dict) -> str`；`write_csv(data: dict, path: str)`；`update_history(history_path: str, data: dict) -> dict`；`write_reports(run, cfg, out_dir=None) -> dict[str, str]`（返回 `{"json": ..., "md": ..., "csv": ..., "history": ...}` 路径）；`regenerate(report_json_path: str) -> dict[str, str]`。CSV 列顺序常量 `CSV_COLUMNS`。

- [ ] **Step 1: 写失败测试**

`tests/test_report.py`：

```python
import csv
import json
from crossborder_selector.config import load_config
from crossborder_selector.models import (Candidate, CandidateScore, IspProbe, ProbeResult,
                                         ReputationResult, RoundResult, RunResult, SourceResult)
from crossborder_selector.report import to_dict, render_markdown, write_reports, update_history, regenerate, CSV_COLUMNS


def _score(iid, ip, prefix, comp, qualified=True, veto=""):
    rep = ReputationResult(ip, [SourceResult("dnsbl", False)], 100.0)
    prs = [ProbeResult("reverse", [IspProbe("telecom", 4, 4, 50.0), IspProbe("unicom", 4, 4, 60.0), IspProbe("mobile", 4, 3, 90.0)]),
           ProbeResult("globalping", [IspProbe("HK", 4, 4, 20.0)]), ProbeResult("ripeatlas", [], "no key")]
    return CandidateScore(Candidate(iid, ip, prefix, 1, "2026-09-07T00:00:00Z"), rep, prs,
                          {"telecom": 95.0, "unicom": 90.0, "mobile": 60.0}, {"reverse": 81.7, "globalping": 100.0},
                          comp, qualified, veto)


def _run():
    a, b = _score("i-1", "18.162.1.1", "18.162.0.0/16", 88.5), _score("i-2", "43.198.2.2", "43.198.0.0/15", 70.0)
    v = CandidateScore(Candidate("i-3", "18.163.3.3", "18.163.0.0/16", 1, ""), ReputationResult("18.163.3.3", [SourceResult("dnsbl", True)], 50.0), [], {}, {}, 0.0, False, "reputation")
    rnd = RoundResult(1, [a.candidate, b.candidate, v.candidate], [v], [a, b], [a], ["i-2", "i-3"], {"ripeatlas": "no key"})
    return RunResult("xb-1", "ap-east-1", [rnd], [a], "2026-09-07T00:00:00Z", "2026-09-07T00:10:00Z", "max_rounds")


def test_to_dict_shape():
    d = to_dict(_run(), load_config(None))
    assert d["run_id"] == "xb-1" and d["region"] == "ap-east-1" and d["stop_reason"] == "max_rounds"
    assert d["winners"][0]["public_ip"] == "18.162.1.1"
    rows = d["candidates"]
    assert len(rows) == 3 and {r["instance_id"] for r in rows} == {"i-1", "i-2", "i-3"}
    r1 = next(r for r in rows if r["instance_id"] == "i-1")
    assert r1["kept"] is True and r1["reverse_telecom"] == 50.0 and r1["globalping_HK"] == 20.0 and r1["ripeatlas_telecom"] is None
    assert d["prefixes"]["18.162.0.0/16"]["samples"] == 1 and d["prefixes"]["18.162.0.0/16"]["best_composite"] == 88.5
    assert d["config"]["batch_size"] == 10 and "abuseipdb_api_key" not in json.dumps(d["config"])


def test_markdown_mentions_winner_and_warning():
    md = render_markdown(to_dict(_run(), load_config(None)))
    assert "18.162.1.1" in md and "i-1" in md and "stop" in md.lower()
    assert "reputation" in md


def test_write_reports_and_regenerate(tmp_path):
    cfg = load_config(None, {"output_dir": str(tmp_path / "out"), "history_file": str(tmp_path / "h.json")})
    paths = write_reports(_run(), cfg)
    assert all((tmp_path / "out" / "xb-1" / n).exists() for n in ("report.json", "report.md", "candidates.csv"))
    with open(paths["csv"]) as f:
        rows = list(csv.DictReader(f))
    assert [c for c in rows[0]] == CSV_COLUMNS and len(rows) == 3
    hist = json.load(open(paths["history"]))
    assert hist["18.162.0.0/16"]["samples"] == 1
    write_reports(_run(), cfg)  # 第二次 run 合并历史
    assert json.load(open(paths["history"]))["18.162.0.0/16"]["samples"] == 2
    (tmp_path / "out" / "xb-1" / "report.md").unlink()
    regenerate(paths["json"])
    assert (tmp_path / "out" / "xb-1" / "report.md").exists()


def test_update_history_merges_best_and_mean(tmp_path):
    p = str(tmp_path / "h.json")
    update_history(p, {"prefixes": {"a/24": {"samples": 2, "mean_composite": 80.0, "best_composite": 90.0}}, "finished_at": "t1"})
    h = update_history(p, {"prefixes": {"a/24": {"samples": 2, "mean_composite": 60.0, "best_composite": 70.0}}, "finished_at": "t2"})
    assert h["a/24"] == {"samples": 4, "mean_composite": 70.0, "best_composite": 90.0, "last_seen": "t2"}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_report.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 report.py**

```python
"""JSON / Markdown / CSV 报告与 prefix 历史。"""
import csv
import json
import os
from dataclasses import asdict
from statistics import mean

from crossborder_selector.config import ISPS

BACKEND_ISP_COLUMNS = [("reverse", i) for i in ISPS] + [("globalping", "HK"), ("globalping", "TW")] + \
                      [("ripeatlas", i) for i in ISPS] + [("itdog", i) for i in ISPS]
CSV_COLUMNS = ["run_id", "round", "instance_id", "public_ip", "prefix", "reputation_score", "veto_reason",
               "composite", "qualified"] + [f"{b}_{i}" for b, i in BACKEND_ISP_COLUMNS] + ["kept", "terminated"]
_SECRET_KEYS = {"api_key", "abuseipdb_api_key"}


def _redact(obj):
    """报告里直接删除密钥字段，不保留键名。"""
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _raw_rtt(score, backend, isp):
    """该 backend 对该 ISP 的原始 rtt 中位数均值（ms）；无数据 None。"""
    for pr in score.probe_results:
        if pr.backend == backend and pr.ok:
            vals = [p.median_rtt_ms for p in pr.probes if p.isp == isp and p.median_rtt_ms is not None]
            return round(mean(vals), 1) if vals else None
    return None


def _row(run, s, kept_ids, terminated_ids):
    c = s.candidate
    row = {"run_id": run.run_id, "round": c.round, "instance_id": c.instance_id, "public_ip": c.public_ip,
           "prefix": c.prefix, "reputation_score": s.reputation.score if s.reputation else None,
           "veto_reason": s.veto_reason, "composite": s.composite, "qualified": s.qualified}
    for b, i in BACKEND_ISP_COLUMNS:
        row[f"{b}_{i}"] = _raw_rtt(s, b, i)
    row["kept"] = c.instance_id in kept_ids
    row["terminated"] = c.instance_id in terminated_ids
    row["isp_scores"], row["backend_scores"] = s.isp_scores, s.backend_scores
    return row


def to_dict(run, cfg) -> dict:
    kept_ids = {w.candidate.instance_id for w in run.winners}
    terminated = {i for r in run.rounds for i in r.terminated}
    scores = [s for r in run.rounds for s in (r.vetoed + r.scored)]
    rows = [_row(run, s, kept_ids, terminated) for s in scores]
    prefixes = {}
    for s in scores:
        if s.candidate.prefix and s.qualified:
            prefixes.setdefault(s.candidate.prefix, []).append(s.composite)
    prefix_stats = {p: {"samples": len(v), "mean_composite": round(mean(v), 2), "best_composite": max(v)}
                    for p, v in prefixes.items()}
    return {
        "run_id": run.run_id, "region": run.region, "started_at": run.started_at, "finished_at": run.finished_at,
        "stop_reason": run.stop_reason, "rounds_completed": len(run.rounds),
        "config": _redact(asdict(cfg)),
        "winners": [_row(run, w, kept_ids, terminated) for w in run.winners],
        "rounds": [{"round": r.round, "launched": len(r.launched), "vetoed": len(r.vetoed),
                    "scored": len(r.scored), "kept": [s.candidate.public_ip for s in r.kept],
                    "terminated": r.terminated, "backend_errors": r.backend_errors} for r in run.rounds],
        "candidates": rows, "prefixes": prefix_stats,
    }


def render_markdown(d: dict) -> str:
    L = [f"# 跨境优选实例报告 {d['run_id']}", "",
         f"- Region：{d['region']}", f"- 时间：{d['started_at']} → {d['finished_at']}",
         f"- 轮数：{d['rounds_completed']}，停止原因：{d['stop_reason']}", "", "## Winners", ""]
    if d["winners"]:
        L += ["| instance | public IP | prefix | composite | 电信 | 联通 | 移动 |", "|---|---|---|---|---|---|---|"]
        for w in d["winners"]:
            s = w["isp_scores"]
            L.append(f"| {w['instance_id']} | {w['public_ip']} | {w['prefix']} | {w['composite']} | "
                     f"{s.get('telecom', '-')} | {s.get('unicom', '-')} | {s.get('mobile', '-')} |")
        L += ["", "> **不要 stop 这些实例。** stop/start 会更换公网 IPv4；reboot 不会。",
              "> 实例已移除 run-id 标签并打上 `crossborder-winner=true`，`cleanup` 不会终止它们。"]
    else:
        L.append("本次没有合格候选。")
    L += ["", "## 每轮概览", "", "| round | launched | vetoed | scored | kept | terminated | backend errors |", "|---|---|---|---|---|---|---|"]
    for r in d["rounds"]:
        L.append(f"| {r['round']} | {r['launched']} | {r['vetoed']} | {r['scored']} | {', '.join(r['kept']) or '-'} | "
                 f"{len(r['terminated'])} | {', '.join(r['backend_errors']) or '-'} |")
    L += ["", "## 全部候选", "", "| round | IP | prefix | composite | qualified | veto |", "|---|---|---|---|---|---|"]
    for c in sorted(d["candidates"], key=lambda x: (-x["composite"], x["round"])):
        L.append(f"| {c['round']} | {c['public_ip']} | {c['prefix']} | {c['composite']} | {c['qualified']} | {c['veto_reason'] or '-'} |")
    L += ["", "## Prefix 统计（合格候选）", "", "| prefix | samples | mean | best |", "|---|---|---|---|"]
    for p, v in sorted(d["prefixes"].items(), key=lambda kv: -kv[1]["best_composite"]):
        L.append(f"| {p} | {v['samples']} | {v['mean_composite']} | {v['best_composite']} |")
    return "\n".join(L) + "\n"


def write_csv(d: dict, path: str):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in d["candidates"]:
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in CSV_COLUMNS})


def update_history(path: str, d: dict) -> dict:
    hist = {}
    if os.path.exists(path):
        with open(path) as f:
            hist = json.load(f)
    for p, v in d.get("prefixes", {}).items():
        h = hist.get(p, {"samples": 0, "mean_composite": 0.0, "best_composite": 0.0, "last_seen": ""})
        n = h["samples"] + v["samples"]
        h["mean_composite"] = round((h["mean_composite"] * h["samples"] + v["mean_composite"] * v["samples"]) / n, 2)
        h["samples"], h["best_composite"] = n, max(h["best_composite"], v["best_composite"])
        h["last_seen"] = d.get("finished_at", "")
        hist[p] = h
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(hist, f, indent=2, ensure_ascii=False)
    return hist


def _write_all(d: dict, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    paths = {"json": os.path.join(out_dir, "report.json"), "md": os.path.join(out_dir, "report.md"),
             "csv": os.path.join(out_dir, "candidates.csv")}
    with open(paths["json"], "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    with open(paths["md"], "w") as f:
        f.write(render_markdown(d))
    write_csv(d, paths["csv"])
    return paths


def write_reports(run, cfg, out_dir=None) -> dict:
    d = to_dict(run, cfg)
    paths = _write_all(d, os.path.join(out_dir or cfg.output_dir, run.run_id))
    update_history(cfg.history_file, d)
    paths["history"] = cfg.history_file
    return paths


def regenerate(report_json_path: str) -> dict:
    with open(report_json_path) as f:
        d = json.load(f)
    return _write_all(d, os.path.dirname(report_json_path))
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_report.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: JSON/Markdown/CSV reports and prefix history"
```

---

### Task 12: Globalping backend（HK/TW）

**Files:**
- Create: `crossborder_selector/probes/globalping.py`
- Test: `tests/test_probe_globalping.py`

**Interfaces:**
- Consumes: `ProbeBackend`、`IspProbe`、`ProbeResult`、`Candidate`。
- Produces: `GlobalpingBackend(backend_cfg: dict, http=None, sleeper=time.sleep, clock=time.time, poll_s=2)`。`http(method: str, url: str, body: dict | None) -> dict`（默认用 requests）。`API_BASE = "https://api.globalping.io/v1"`。

- [ ] **Step 1: 写失败测试**

`tests/test_probe_globalping.py`：

```python
from crossborder_selector.models import Candidate
from crossborder_selector.probes.globalping import GlobalpingBackend, API_BASE

CFG = {"enabled": True, "locations": ["HK", "TW"], "limit_per_location": 2, "packets": 4, "timeout_s": 60}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def _finished(ip):
    return {"id": "m1", "status": "finished", "results": [
        {"probe": {"location": {"country": "HK"}}, "result": {"status": "finished", "stats": {"total": 4, "rcv": 4, "avg": 12.5}}},
        {"probe": {"location": {"country": "HK"}}, "result": {"status": "finished", "stats": {"total": 4, "rcv": 3, "avg": 15.0}}},
        {"probe": {"location": {"country": "TW"}}, "result": {"status": "finished", "stats": {"total": 4, "rcv": 0, "avg": None}}},
    ]}


class FakeHttp:
    def __init__(self, polls_before_finish=1):
        self.calls, self.n = [], polls_before_finish
    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if method == "POST":
            return {"id": "m1", "probesCount": 3}
        self.n -= 1
        return {"id": "m1", "status": "in-progress", "results": []} if self.n >= 0 else _finished("x")


def test_request_body_and_parsing():
    clk, http = FakeClock(), FakeHttp()
    b = GlobalpingBackend(CFG, http=http, sleeper=clk.sleep, clock=clk)
    out = b.probe([Candidate("i-1", "18.162.1.1")])
    m, url, body = http.calls[0]
    assert m == "POST" and url == f"{API_BASE}/measurements"
    assert body["type"] == "ping" and body["target"] == "18.162.1.1"
    assert body["locations"] == [{"country": "HK", "limit": 2}, {"country": "TW", "limit": 2}]
    assert body["measurementOptions"] == {"packets": 4}
    pr = out["18.162.1.1"]
    assert pr.ok and [p.isp for p in pr.probes] == ["HK", "HK", "TW"]
    assert pr.probes[0].median_rtt_ms == 12.5 and pr.probes[2].received == 0 and pr.probes[2].median_rtt_ms is None


def test_timeout_yields_error():
    clk = FakeClock()
    b = GlobalpingBackend({**CFG, "timeout_s": 5}, http=FakeHttp(polls_before_finish=99), sleeper=clk.sleep, clock=clk, poll_s=2)
    pr = b.probe([Candidate("i-1", "1.1.1.1")])["1.1.1.1"]
    assert not pr.ok and "timeout" in pr.error


def test_http_error_isolated_per_candidate():
    def http(m, u, b=None):
        if b and b["target"] == "2.2.2.2":
            raise RuntimeError("429 too many")
        return {"id": "m1"} if m == "POST" else _finished("x")
    clk = FakeClock()
    out = GlobalpingBackend(CFG, http=http, sleeper=clk.sleep, clock=clk).probe([Candidate("a", "1.1.1.1"), Candidate("b", "2.2.2.2")])
    assert out["1.1.1.1"].ok and "429" in out["2.2.2.2"].error
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_probe_globalping.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 globalping.py**

```python
"""Globalping 公共探针：从 HK/TW 视角 ping 候选 IP。无需 API key。"""
import time

import requests

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

API_BASE = "https://api.globalping.io/v1"


def _default_http(method, url, body=None):
    r = requests.request(method, url, json=body, timeout=20,
                         headers={"Content-Type": "application/json", "User-Agent": "crossborder-selector"})
    r.raise_for_status()
    return r.json()


class GlobalpingBackend(ProbeBackend):
    name = "globalping"

    def __init__(self, backend_cfg, http=None, sleeper=time.sleep, clock=time.time, poll_s=2):
        self.cfg, self.http = backend_cfg, http or _default_http
        self._sleep, self._now, self.poll_s = sleeper, clock, poll_s

    def _measure(self, ip) -> ProbeResult:
        body = {"type": "ping", "target": ip,
                "locations": [{"country": c, "limit": self.cfg["limit_per_location"]} for c in self.cfg["locations"]],
                "measurementOptions": {"packets": self.cfg["packets"]}}
        mid = self.http("POST", f"{API_BASE}/measurements", body)["id"]
        deadline = self._now() + self.cfg["timeout_s"]
        while True:
            self._sleep(self.poll_s)
            data = self.http("GET", f"{API_BASE}/measurements/{mid}")
            if data.get("status") == "finished":
                break
            if self._now() >= deadline:
                return ProbeResult(self.name, [], f"globalping timeout for measurement {mid}")
        probes = []
        for r in data.get("results", []):
            st = (r.get("result") or {}).get("stats") or {}
            probes.append(IspProbe(isp=r["probe"]["location"]["country"], sent=int(st.get("total") or 0),
                                   received=int(st.get("rcv") or 0),
                                   median_rtt_ms=(None if st.get("avg") is None else float(st["avg"])),
                                   target=ip, method="ping"))
        return ProbeResult(self.name, probes)

    def probe(self, candidates) -> dict:
        out = {}
        for c in candidates:  # 串行提交，遵守公共 API 速率限制
            try:
                out[c.public_ip] = self._measure(c.public_ip)
            except Exception as e:
                out[c.public_ip] = ProbeResult(self.name, [], f"{type(e).__name__}: {e}")
        return out
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_probe_globalping.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: Globalping HK/TW probe backend"
```

---

### Task 13: RIPE Atlas backend（大陆探针）

**Files:**
- Create: `crossborder_selector/probes/ripeatlas.py`
- Test: `tests/test_probe_ripeatlas.py`

**Interfaces:**
- Consumes: 同 Task 12。
- Produces: `RipeAtlasBackend(backend_cfg, http=None, sleeper, clock, poll_s=10)`；`isp_for_asn(asn: int) -> str`（返回 telecom/unicom/mobile/other）；`ASN_ISP` 映射表；`API_BASE = "https://atlas.ripe.net/api/v2"`。`http(method, url, body=None, params=None) -> dict | list`。

- [ ] **Step 1: 写失败测试**

`tests/test_probe_ripeatlas.py`：

```python
from crossborder_selector.models import Candidate
from crossborder_selector.probes.ripeatlas import RipeAtlasBackend, isp_for_asn, API_BASE

CFG = {"enabled": True, "api_key": "KEY", "probe_count": 3, "packets": 4, "timeout_s": 120}


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class FakeHttp:
    def __init__(self):
        self.calls, self.polls = [], 0
    def __call__(self, method, url, body=None, params=None):
        self.calls.append((method, url, body, params))
        if method == "POST":
            return {"measurements": [77]}
        if url.endswith("/measurements/77/results/"):
            self.polls += 1
            if self.polls == 1:
                return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 180.0}]
            return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 180.0},
                    {"prb_id": 2, "sent": 4, "rcvd": 2, "avg": 240.0},
                    {"prb_id": 3, "sent": 4, "rcvd": 0, "avg": -1}]
        if url.endswith("/probes/1/"):
            return {"id": 1, "asn_v4": 4134}
        if url.endswith("/probes/2/"):
            return {"id": 2, "asn_v4": 9808}
        if url.endswith("/probes/3/"):
            return {"id": 3, "asn_v4": 12345}
        raise AssertionError(url)


def test_asn_mapping():
    assert isp_for_asn(4134) == "telecom" and isp_for_asn(4837) == "unicom"
    assert isp_for_asn(56046) == "mobile" and isp_for_asn(99999) == "other"


def test_create_poll_and_map():
    clk, http = FakeClock(), FakeHttp()
    b = RipeAtlasBackend(CFG, http=http, sleeper=clk.sleep, clock=clk)
    pr = b.probe([Candidate("i", "18.162.1.1")])["18.162.1.1"]
    m, url, body, params = http.calls[0]
    assert m == "POST" and url == f"{API_BASE}/measurements/" and params == {"key": "KEY"}
    d = body["definitions"][0]
    assert d["target"] == "18.162.1.1" and d["type"] == "ping" and d["af"] == 4 and d["packets"] == 4
    assert body["probes"] == [{"type": "country", "value": "CN", "requested": 3}] and body["is_oneoff"] is True
    assert pr.ok and [p.isp for p in pr.probes] == ["telecom", "mobile", "other"]
    assert pr.probes[2].median_rtt_ms is None and pr.probes[1].received == 2


def test_partial_results_on_timeout_still_ok():
    class Slow(FakeHttp):
        def __call__(self, method, url, body=None, params=None):
            if url.endswith("/results/"):
                self.calls.append((method, url, body, params))
                return [{"prb_id": 1, "sent": 4, "rcvd": 4, "avg": 100.0}]
            return super().__call__(method, url, body, params)
    clk = FakeClock()
    pr = RipeAtlasBackend({**CFG, "timeout_s": 25}, http=Slow(), sleeper=clk.sleep, clock=clk).probe([Candidate("i", "1.1.1.1")])["1.1.1.1"]
    assert pr.ok and len(pr.probes) == 1


def test_no_results_is_error():
    class Empty(FakeHttp):
        def __call__(self, method, url, body=None, params=None):
            return {"measurements": [77]} if method == "POST" else []
    clk = FakeClock()
    pr = RipeAtlasBackend({**CFG, "timeout_s": 20}, http=Empty(), sleeper=clk.sleep, clock=clk).probe([Candidate("i", "1.1.1.1")])["1.1.1.1"]
    assert not pr.ok and "no results" in pr.error
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_probe_ripeatlas.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 ripeatlas.py**

```python
"""RIPE Atlas：从大陆在线探针 one-off ping 候选 IP。需要 API key（免费申请 credits）。"""
import time

import requests

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

API_BASE = "https://atlas.ripe.net/api/v2"
ASN_ISP = {
    "telecom": {4134, 4812, 23724, 4809, 58466, 134762},
    "unicom": {4837, 4808, 17816, 17622, 4847, 9929},
    "mobile": {9808, 56040, 56041, 56042, 56044, 56045, 56046, 56047, 56048, 24444, 24445, 24547, 9394},
}


def isp_for_asn(asn) -> str:
    for isp, asns in ASN_ISP.items():
        if asn in asns:
            return isp
    return "other"


def _default_http(method, url, body=None, params=None):
    r = requests.request(method, url, json=body, params=params, timeout=20,
                         headers={"User-Agent": "crossborder-selector"})
    r.raise_for_status()
    return r.json()


class RipeAtlasBackend(ProbeBackend):
    name = "ripeatlas"

    def __init__(self, backend_cfg, http=None, sleeper=time.sleep, clock=time.time, poll_s=10):
        self.cfg, self.http = backend_cfg, http or _default_http
        self._sleep, self._now, self.poll_s = sleeper, clock, poll_s
        self._asn_cache = {}

    def _asn(self, prb_id) -> int:
        if prb_id not in self._asn_cache:
            self._asn_cache[prb_id] = int(self.http("GET", f"{API_BASE}/probes/{prb_id}/").get("asn_v4") or 0)
        return self._asn_cache[prb_id]

    def _measure(self, ip) -> ProbeResult:
        body = {"definitions": [{"target": ip, "af": 4, "type": "ping", "packets": self.cfg["packets"],
                                 "description": f"crossborder-selector {ip}", "is_oneoff": True}],
                "probes": [{"type": "country", "value": "CN", "requested": self.cfg["probe_count"]}],
                "is_oneoff": True}
        mid = self.http("POST", f"{API_BASE}/measurements/", body, {"key": self.cfg["api_key"]})["measurements"][0]
        deadline, results = self._now() + self.cfg["timeout_s"], []
        while True:
            self._sleep(self.poll_s)
            results = self.http("GET", f"{API_BASE}/measurements/{mid}/results/") or []
            if len(results) >= self.cfg["probe_count"] or self._now() >= deadline:
                break
        if not results:
            return ProbeResult(self.name, [], f"ripeatlas no results for measurement {mid}")
        probes = []
        for r in results:
            avg = r.get("avg")
            probes.append(IspProbe(isp=isp_for_asn(self._asn(r["prb_id"])), sent=int(r.get("sent") or 0),
                                   received=int(r.get("rcvd") or 0),
                                   median_rtt_ms=(None if avg is None or avg < 0 else float(avg)),
                                   target=ip, method="ping"))
        return ProbeResult(self.name, probes)

    def probe(self, candidates) -> dict:
        out = {}
        for c in candidates:
            try:
                out[c.public_ip] = self._measure(c.public_ip)
            except Exception as e:
                out[c.public_ip] = ProbeResult(self.name, [], f"{type(e).__name__}: {e}")
        return out
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_probe_ripeatlas.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: RIPE Atlas mainland-probe backend"
```

---

### Task 14: itdog backend（非官方协议，默认关闭）

**Files:**
- Create: `crossborder_selector/probes/itdog.py`
- Test: `tests/test_probe_itdog.py`

**Interfaces:**
- Consumes: `ProbeBackend`、`IspProbe`、`ProbeResult`。
- Produces: `ItdogBackend(backend_cfg, session=None, ws_connect=None, clock=time.time)`；纯函数 `generate_guardret(guard: str) -> str`、`task_token(task_id: str) -> str`、`parse_page(html) -> tuple[str, str]`（返回 `(wss_url, task_id)`，缺失抛 `ValueError`）。`session` 需有 `cookies`（dict-like）与 `post(url, headers=, data=) -> obj(.text)`；`ws_connect(url)` 返回上下文管理器，对象有 `send(str)` 与 `recv() -> str`。

协议要点（来自社区实现，可能随时失效）：
- `POST https://www.itdog.cn/batch_ping/`，表单 `host`（多 IP 以 `\r\n` 连接）、`node_id`（逗号分隔）、`cidr_filter=true`、`gateway=last`；头 `Referer: https://www.itdog.cn/batch_ping/`、`Content-Type: application/x-www-form-urlencoded`、浏览器 UA。
- 反爬：首次 POST 返回 `guard` cookie；需计算 `guardret` 再 POST 一次。算法：`key = guard[:8]`，`num = int(guard[12:])`，`value = num*2+16`，对 `str(value)` 用 `key + "PTNo2n3Ev5"` 循环 XOR，再 base64。
- 页面 HTML 中 `var wss_url='...';` 与 `var task_id='...';`。
- `task_token = md5(task_id + "token_20230313000136kwyktxb0tgspm00yo5").hexdigest()[8:-8]`。
- WebSocket 发送 `{"task_id":..,"task_token":..}`；收到 JSON 消息，`type == "finished"` 结束；其余含 `ip`、`result`（毫秒字符串，非数字表示失败）、`node_id`。

- [ ] **Step 1: 写失败测试**

`tests/test_probe_itdog.py`：

```python
import base64
import hashlib
import json
import pytest
from crossborder_selector.models import Candidate
from crossborder_selector.probes.itdog import ItdogBackend, generate_guardret, task_token, parse_page

CFG = {"enabled": True, "timeout_s": 30, "nodes": {"1310": "telecom", "1273": "unicom", "1250": "mobile"}}
HTML = "<script>var wss_url='wss://ws.itdog.cn/x';var task_id='abc123';</script>"


def test_task_token_matches_reference():
    ref = hashlib.md5(("abc123" + "token_20230313000136kwyktxb0tgspm00yo5").encode()).hexdigest()[8:-8]
    assert task_token("abc123") == ref


def test_guardret_reference_algorithm():
    guard = "abcdefgh1234" + "21"
    key = "abcdefgh" + "PTNo2n3Ev5"
    val = str(21 * 2 + 16)
    enc = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(val))
    assert generate_guardret(guard) == base64.b64encode(enc.encode()).decode()


def test_parse_page():
    assert parse_page(HTML) == ("wss://ws.itdog.cn/x", "abc123")
    with pytest.raises(ValueError):
        parse_page("<html/>")


class Resp:
    def __init__(self, text): self.text = text


class FakeSession:
    def __init__(self):
        self.cookies, self.posts = {}, []
    def post(self, url, headers=None, data=None):
        self.posts.append((url, headers, data))
        if "guard" not in self.cookies:
            self.cookies["guard"] = "abcdefgh123421"
            return Resp("blocked")
        return Resp(HTML)


class FakeWs:
    def __init__(self, msgs): self.msgs, self.sent = list(msgs), []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def send(self, s): self.sent.append(s)
    def recv(self):
        if not self.msgs:
            raise TimeoutError("closed")
        return self.msgs.pop(0)


def test_full_flow_maps_nodes_to_isps():
    msgs = [json.dumps({"type": "data", "ip": "1.1.1.1", "result": "45", "node_id": "1310"}),
            json.dumps({"type": "data", "ip": "1.1.1.1", "result": "超时", "node_id": "1273"}),
            json.dumps({"type": "data", "ip": "2.2.2.2", "result": "80", "node_id": "1250"}),
            json.dumps({"type": "data", "ip": "1.1.1.1", "result": "70", "node_id": "9999"}),
            json.dumps({"type": "finished"})]
    ws, sess = FakeWs(msgs), FakeSession()
    b = ItdogBackend(CFG, session=sess, ws_connect=lambda url: ws)
    out = b.probe([Candidate("a", "1.1.1.1"), Candidate("b", "2.2.2.2")])
    assert len(sess.posts) == 2 and sess.cookies["guardret"] == generate_guardret("abcdefgh123421")
    assert sess.posts[1][2]["host"] == "1.1.1.1\r\n2.2.2.2" and sess.posts[1][2]["node_id"] == "1310,1273,1250"
    assert json.loads(ws.sent[0]) == {"task_id": "abc123", "task_token": task_token("abc123")}
    p1 = out["1.1.1.1"].probes
    assert [(p.isp, p.received, p.median_rtt_ms) for p in p1] == [("telecom", 1, 45.0), ("unicom", 0, None)]
    assert out["2.2.2.2"].probes[0].isp == "mobile"


def test_protocol_change_degrades_to_error():
    class Broken(FakeSession):
        def post(self, url, headers=None, data=None):
            self.cookies["guard"] = "abcdefgh123421"
            return Resp("<html>changed</html>")
    b = ItdogBackend(CFG, session=Broken(), ws_connect=lambda url: FakeWs([]))
    out = b.probe([Candidate("a", "1.1.1.1")])
    assert not out["1.1.1.1"].ok and "itdog" in out["1.1.1.1"].error
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_probe_itdog.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 itdog.py**

```python
"""itdog.cn 批量 ping 的非官方封装。协议来自社区逆向，随时可能失效；任何失败只记 error。"""
import base64
import hashlib
import json
import re
import time

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

BATCH_PING_URL = "https://www.itdog.cn/batch_ping/"
TASK_TOKEN_SECRET = "token_20230313000136kwyktxb0tgspm00yo5"
GUARD_XOR_SUFFIX = "PTNo2n3Ev5"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_RE_WSS = re.compile(r"var wss_url='(.*?)';")
_RE_TASK = re.compile(r"var task_id='(.*?)';")


def generate_guardret(guard: str) -> str:
    key = guard[:8] + GUARD_XOR_SUFFIX
    num = int(guard[12:]) if len(guard) > 12 else 0
    value = str(num * 2 + 18 - 2)
    enc = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(value))
    return base64.b64encode(enc.encode()).decode()


def task_token(task_id: str) -> str:
    return hashlib.md5((task_id + TASK_TOKEN_SECRET).encode()).hexdigest()[8:-8]


def parse_page(html: str):
    m1, m2 = _RE_WSS.search(html or ""), _RE_TASK.search(html or "")
    if not m1 or not m2:
        raise ValueError("itdog page missing wss_url/task_id (protocol changed?)")
    return m1.group(1), m2.group(1)


def _default_ws_connect(url):
    from websockets.sync.client import connect  # 延迟导入：只有启用 itdog 才需要该依赖
    return connect(url, open_timeout=10, close_timeout=5)


class ItdogBackend(ProbeBackend):
    name = "itdog"

    def __init__(self, backend_cfg, session=None, ws_connect=None, clock=time.time):
        self.cfg, self._now = backend_cfg, clock
        self.nodes = {str(k): v for k, v in backend_cfg["nodes"].items()}
        if session is None:
            import requests
            session = requests.Session()
        self.session, self.ws_connect = session, ws_connect or _default_ws_connect

    def _start_task(self, ips):
        headers = {"User-Agent": UA, "Referer": BATCH_PING_URL,
                   "Content-Type": "application/x-www-form-urlencoded"}
        data = {"host": "\r\n".join(ips), "node_id": ",".join(self.nodes), "cidr_filter": "true", "gateway": "last"}
        resp = self.session.post(BATCH_PING_URL, headers=headers, data=data)
        if "guardret" not in self.session.cookies and "guard" in self.session.cookies:
            self.session.cookies["guardret"] = generate_guardret(self.session.cookies["guard"])
            resp = self.session.post(BATCH_PING_URL, headers=headers, data=data)
        return parse_page(resp.text)

    def _collect(self, wss_url, task_id, ips) -> dict:
        samples = {ip: [] for ip in ips}
        deadline = self._now() + self.cfg["timeout_s"]
        with self.ws_connect(wss_url) as ws:
            ws.send(json.dumps({"task_id": task_id, "task_token": task_token(task_id)}))
            while self._now() < deadline:
                try:
                    msg = json.loads(ws.recv())
                except (TimeoutError, OSError, ValueError):
                    break
                if msg.get("type") == "finished":
                    break
                ip, node = msg.get("ip"), str(msg.get("node_id", ""))
                if ip not in samples or node not in self.nodes:
                    continue
                raw = str(msg.get("result", "")).strip()
                try:
                    rtt, ok = float(raw), 1
                except ValueError:
                    rtt, ok = None, 0
                samples[ip].append(IspProbe(isp=self.nodes[node], sent=1, received=ok, median_rtt_ms=rtt,
                                            target=f"node:{node}", method="ping"))
        return samples

    def probe(self, candidates) -> dict:
        ips = [c.public_ip for c in candidates]
        try:
            wss_url, task_id = self._start_task(ips)
            samples = self._collect(wss_url, task_id, ips)
        except Exception as e:
            return {ip: ProbeResult(self.name, [], f"itdog failed: {type(e).__name__}: {e}") for ip in ips}
        return {ip: (ProbeResult(self.name, s) if s else ProbeResult(self.name, [], "itdog returned no samples"))
                for ip, s in samples.items()}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_probe_itdog.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: itdog unofficial probe backend (opt-in)"
```

---

### Task 15: CLI（select / cleanup / report）

**Files:**
- Create: `crossborder_selector/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: 前面所有模块。
- Produces: `new_run_id() -> str`；`build_backends(cfg, ssm_runner) -> list[ProbeBackend]`；`plan_summary(cfg, run_id) -> str`（dry-run 文本）；`main(argv=None) -> int`；内部 `_do_select(args, factory)`、`_do_cleanup(args, factory)`、`_do_report(args)`。`factory(cfg) -> dict` 返回 `{"ec2": boto3 ec2 client, "iam": ..., "ssm": ...}`，测试可替换。

- [ ] **Step 1: 写失败测试**

`tests/test_cli.py`：

```python
import json
import re
import boto3
import pytest
from moto import mock_aws

from crossborder_selector import cli
from crossborder_selector.aws.infra import AMI_PARAMS
from crossborder_selector.config import load_config

REGION = "us-east-1"


def test_run_id_format():
    assert re.fullmatch(r"xb-\d{8}T\d{6}Z-[0-9a-f]{4}", cli.new_run_id())


def test_build_backends_respects_enable_flags():
    cfg = load_config(None, {"enable_backends": ["itdog"], "disable_backends": ["globalping"]})
    names = [b.name for b in cli.build_backends(cfg, ssm_runner=object())]
    assert names == ["reverse", "itdog"]
    cfg2 = load_config(None, {"backends": {"ripeatlas": {"enabled": True, "api_key": ""}}})
    assert "ripeatlas" not in [b.name for b in cli.build_backends(cfg2, ssm_runner=object())]


def test_dry_run_prints_plan_without_clients(capsys):
    def no_factory(cfg):
        raise AssertionError("dry-run must not create clients")
    rc = cli.main(["select", "--dry-run", "--batch-size", "3", "--region", "ap-east-1"], factory=no_factory)
    out = capsys.readouterr().out
    assert rc == 0 and "DRY-RUN" in out and "ap-east-1" in out and "3 x t3.nano" in out and "reverse" in out


def test_select_rejects_bad_override():
    with pytest.raises(SystemExit):
        cli.main(["select", "--dry-run", "--enable-backend", "nope"])


@mock_aws
def test_cleanup_terminates_only_run_and_keeps_infra(capsys):
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name=AMI_PARAMS["x86_64"], Value=ec2.describe_images()["Images"][0]["ImageId"], Type="String")
    from crossborder_selector.aws.infra import ensure_infra, SG_NAME
    from crossborder_selector.aws.ec2 import Ec2Manager
    cfg = load_config(None, {"region": REGION})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    a = m.launch(2, "xb-a", 1, infra, "t3.nano")
    b = m.launch(1, "xb-b", 1, infra, "t3.nano")
    factory = lambda cfg: {"ec2": ec2, "iam": iam, "ssm": ssm}
    assert cli.main(["cleanup", "--region", REGION, "--run-id", "xb-a"], factory=factory) == 0
    assert m.list_run_instances("xb-a") == [] and set(m.list_run_instances("xb-b")) == set(b)
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]
    assert cli.main(["cleanup", "--region", REGION, "--run-id", "xb-b", "--include-infra"], factory=factory) == 0
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"] == []


def test_report_regenerates(tmp_path, capsys):
    d = {"run_id": "xb-r", "region": "r", "started_at": "", "finished_at": "", "stop_reason": "x", "rounds_completed": 0,
         "config": {}, "winners": [], "rounds": [], "candidates": [], "prefixes": {}}
    out = tmp_path / "out" / "xb-r"
    out.mkdir(parents=True)
    (out / "report.json").write_text(json.dumps(d))
    assert cli.main(["report", "--run-id", "xb-r", "--output-dir", str(tmp_path / "out")]) == 0
    assert (out / "report.md").exists() and (out / "candidates.csv").exists()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 cli.py**

```python
"""命令行入口：select（多轮选机）、cleanup（按 run-id 清理）、report（重生成报告）。"""
import argparse
import os
import secrets
import sys
from datetime import datetime, timezone

import boto3

from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import ensure_infra, delete_infra
from crossborder_selector.aws.ipranges import load_ip_ranges, PrefixLookup
from crossborder_selector.aws.ssm import SsmRunner
from crossborder_selector.config import load_config
from crossborder_selector.orchestrator import Orchestrator
from crossborder_selector.probes.globalping import GlobalpingBackend
from crossborder_selector.probes.itdog import ItdogBackend
from crossborder_selector.probes.reverse import ReverseBackend
from crossborder_selector.probes.ripeatlas import RipeAtlasBackend
from crossborder_selector.report import write_reports, regenerate
from crossborder_selector.reputation.abuseipdb import build_sources


def new_run_id() -> str:
    return f"xb-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


def default_factory(cfg) -> dict:
    return {name: boto3.client(name, region_name=cfg.region) for name in ("ec2", "iam", "ssm")}


def build_backends(cfg, ssm_runner) -> list:
    b, out = cfg.backends, []
    if b["reverse"]["enabled"]:
        out.append(ReverseBackend(ssm_runner, b["reverse"]))
    if b["globalping"]["enabled"]:
        out.append(GlobalpingBackend(b["globalping"]))
    if b["ripeatlas"]["enabled"] and (b["ripeatlas"].get("api_key") or "").strip():
        out.append(RipeAtlasBackend(b["ripeatlas"]))
    if b["itdog"]["enabled"]:
        out.append(ItdogBackend(b["itdog"]))
    return out


def plan_summary(cfg, run_id) -> str:
    enabled = [n for n, v in cfg.backends.items() if v["enabled"]
               and not (n == "ripeatlas" and not (v.get("api_key") or "").strip())]
    lines = [f"DRY-RUN run-id={run_id}", f"region={cfg.region}",
             f"per round: {cfg.batch_size} x {cfg.instance_type}, max_rounds={cfg.max_rounds}, "
             f"keep_top_k={cfg.keep_top_k}, target_score={cfg.target_score}",
             f"infra: subnet={cfg.subnet_id or '<default VPC>'} sg={cfg.security_group_id or 'crossborder-selector-sg'} "
             f"profile={cfg.instance_profile_name or 'crossborder-selector-ssm'} ami={cfg.image_id or '<AL2023 latest>'}",
             f"backends: {', '.join(enabled)}", f"protect winner: {cfg.protect}",
             "reverse targets: " + "; ".join(f"{k}={','.join(v)}" for k, v in cfg.backends['reverse']['targets'].items()),
             "No AWS resources will be created."]
    return "\n".join(lines)


def _overrides(args) -> dict:
    o = {}
    for k in ("region", "batch_size", "max_rounds", "keep_top_k", "target_score", "instance_type"):
        v = getattr(args, k, None)
        if v is not None:
            o[k] = v
    if getattr(args, "protect", False):
        o["protect"] = True
    if getattr(args, "enable_backend", None):
        o["enable_backends"] = args.enable_backend
    if getattr(args, "disable_backend", None):
        o["disable_backends"] = args.disable_backend
    return o


def _load(args):
    try:
        return load_config(args.config, _overrides(args))
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)


def _do_select(args, factory) -> int:
    cfg, run_id = _load(args), new_run_id()
    if args.dry_run:
        print(plan_summary(cfg, run_id))
        return 0
    print(f"run-id: {run_id}  (cleanup: python -m crossborder_selector.cli cleanup --region {cfg.region} --run-id {run_id})")
    clients = factory(cfg)
    infra = ensure_infra(clients["ec2"], clients["iam"], clients["ssm"], cfg)
    ssm_runner = SsmRunner(clients["ssm"])
    prefixes = load_ip_ranges(cache_path=os.path.join(cfg.output_dir, "ip-ranges.json"))
    orch = Orchestrator(cfg, Ec2Manager(clients["ec2"]), ssm_runner, build_backends(cfg, ssm_runner),
                        build_sources(cfg.reputation), PrefixLookup(prefixes, cfg.region), infra, run_id)
    try:
        result = orch.run()
    except Exception as e:
        print(f"run failed: {e}. Non-winner instances were terminated; verify with cleanup --run-id {run_id}",
              file=sys.stderr)
        return 1
    paths = write_reports(result, cfg)
    print(f"stop reason: {result.stop_reason}")
    for w in result.winners:
        print(f"WINNER {w.candidate.instance_id} {w.candidate.public_ip} prefix={w.candidate.prefix} score={w.composite}")
    print(f"reports: {paths['json']}  {paths['md']}  {paths['csv']}")
    print(f"run-id: {run_id}")
    return 0


def _do_cleanup(args, factory) -> int:
    cfg = _load(args)
    clients = factory(cfg)
    m = Ec2Manager(clients["ec2"])
    ids = m.list_run_instances(args.run_id)
    m.terminate(ids)
    print(f"terminated {len(ids)} instance(s) tagged crossborder-run-id={args.run_id}: {ids}")
    if args.include_infra:
        if m.has_winners():
            print("winners exist; refusing to delete shared SG / instance profile", file=sys.stderr)
            return 1
        delete_infra(clients["ec2"], clients["iam"])
        print("deleted crossborder-selector SG and instance profile")
    return 0


def _do_report(args) -> int:
    path = os.path.join(args.output_dir, args.run_id, "report.json")
    paths = regenerate(path)
    print(f"regenerated: {paths['md']}  {paths['csv']}")
    return 0


def _parser():
    p = argparse.ArgumentParser(prog="crossborder-selector")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select", help="多轮启动候选 EC2 并保留跨境最优实例")
    s.add_argument("--config"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--region"); s.add_argument("--instance-type")
    for k in ("--batch-size", "--max-rounds", "--keep-top-k"):
        s.add_argument(k, type=int)
    s.add_argument("--target-score", type=float)
    s.add_argument("--enable-backend", action="append"); s.add_argument("--disable-backend", action="append")
    s.add_argument("--protect", action="store_true", help="对 winner 开启 stop/termination protection")
    c = sub.add_parser("cleanup", help="终止某 run-id 的全部候选机")
    c.add_argument("--config"); c.add_argument("--region"); c.add_argument("--run-id", required=True)
    c.add_argument("--include-infra", action="store_true")
    r = sub.add_parser("report", help="从 report.json 重生成 md/csv")
    r.add_argument("--run-id", required=True); r.add_argument("--output-dir", default="./out")
    return p


def main(argv=None, factory=default_factory) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "select":
        return _do_select(args, factory)
    if args.cmd == "cleanup":
        return _do_cleanup(args, factory)
    return _do_report(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_cli.py -v && pytest -q`
Expected: 6 passed；全量测试全部通过。若 moto 在 `--include-infra` 用例删除 SG 时报 `DependencyViolation`（刚终止的实例仍关联 SG），在该测试里于 cleanup 前再次 `describe_instances` 确认 xb-b 实例状态为 terminated 后重跑；仍失败则改为断言 `delete_infra` 在无 winner 时被调用（用 monkeypatch 替换 `cli.delete_infra`）。

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: CLI with select/cleanup/report and dry-run"
```

---

### Task 16: 包装脚本、文档、冒烟步骤

**Files:**
- Create: `scripts/find_best_instance.sh`、`README.md`、`MANUAL.md`
- Test: `tests/test_wrapper_script.py`

**Interfaces:**
- Produces: `scripts/find_best_instance.sh [region] [batch] [rounds] [keep] [extra args...]`，把参数翻译为 `python -m crossborder_selector.cli select --region .. --batch-size .. --max-rounds .. --keep-top-k .. <extra>`；自动激活 `.venv`。

- [ ] **Step 1: 写失败测试**

`tests/test_wrapper_script.py`：

```python
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "find_best_instance.sh")


def test_wrapper_dry_run():
    env = {**os.environ, "PYTHON_BIN": sys.executable, "PYTHONPATH": ROOT}
    r = subprocess.run(["bash", SCRIPT, "ap-east-1", "4", "2", "1", "--dry-run"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout and "4 x t3.nano" in r.stdout and "max_rounds=2" in r.stdout


def test_wrapper_usage_on_no_args():
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 2 and "usage" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_wrapper_script.py -v`
Expected: FAIL，脚本不存在

- [ ] **Step 3: 写脚本**

`scripts/find_best_instance.sh`：

```bash
#!/usr/bin/env bash
# 一条命令选出跨境最优 EC2 实例。
# 用法: scripts/find_best_instance.sh <region> [batch=10] [rounds=3] [keep=1] [额外 cli 参数...]
# 例:   scripts/find_best_instance.sh ap-east-1 20 3 1 --protect
#       scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ $# -lt 1 ]; then
  echo "usage: $0 <region> [batch=10] [rounds=3] [keep=1] [extra args...]" >&2
  exit 2
fi
REGION="$1"; BATCH="${2:-10}"; ROUNDS="${3:-3}"; KEEP="${4:-1}"
shift; [ $# -gt 0 ] && shift; [ $# -gt 0 ] && shift; [ $# -gt 0 ] && shift
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
cd "$ROOT"
exec "$PY" -m crossborder_selector.cli select --region "$REGION" --batch-size "$BATCH" \
  --max-rounds "$ROUNDS" --keep-top-k "$KEEP" "$@"
```

```bash
chmod +x scripts/find_best_instance.sh
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_wrapper_script.py -v`
Expected: 2 passed

- [ ] **Step 5: 写 README.md**

包含以下小节，每节 3～10 行：

1. **背景**：为什么是"保留实例"而不是 EIP（自动公网 IPv4 不能转 EIP；EIP 分配器重复返回同一地址；配额 5）。reboot 保留 IP，stop/start 丢 IP。
2. **一句话用法**：`scripts/find_best_instance.sh ap-east-1 20 3 1`，以及 `--dry-run`。
3. **流程图**：复制 spec 第 2 节的 ASCII 流程。
4. **拨测数据源**：表格列出 reverse / globalping / ripeatlas / itdog 的视角、默认开关、是否需要 key、局限（Globalping 无大陆探针；RIPE Atlas 在线探针少；itdog 非官方随时失效；reverse 是机房出境近似）。
5. **打分**：spec 第 6 节摘要。
6. **前置条件**：AWS 凭证需 EC2/IAM/SSM 权限（列出最小 action 清单：`ec2:RunInstances`、`ec2:Describe*`、`ec2:TerminateInstances`、`ec2:CreateTags`、`ec2:DeleteTags`、`ec2:CreateSecurityGroup`、`ec2:ModifyInstanceAttribute`、`iam:CreateRole`、`iam:AttachRolePolicy`、`iam:CreateInstanceProfile`、`iam:AddRoleToInstanceProfile`、`iam:PassRole`、`iam:Get*`、`ssm:SendCommand`、`ssm:GetCommandInvocation`、`ssm:DescribeInstanceInformation`、`ssm:GetParameters`）；默认 VPC；vCPU 配额 ≥ batch_size×2。
7. **成本**：spec 第 2 节成本量级。
8. **winner 注意事项**：不要 stop；标签含义；`--protect`；如何手动解除 protection。
9. **安全边界**：无入站 SG、无 SSH、无凭证下发、run-id 标签隔离。
10. **局限与非目标**：spec 第 1 节非目标。

- [ ] **Step 6: 写 MANUAL.md**

按操作顺序：

1. 安装：`python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pytest -q`。
2. 配置：复制 `config.example.yaml` 为 `config.yaml`，逐字段说明；何时需要填 `subnet_id`/`security_group_id`（无默认 VPC）。
3. Dry-run：`scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run`，解释每行输出。
4. 冒烟：`scripts/find_best_instance.sh ap-east-1 2 1 1`，预期 5～8 分钟；如何读 `out/<run-id>/report.md`；确认控制台里只剩 1 台带 `crossborder-winner=true` 的实例。
5. 正式运行：`ap-east-1 20 3 1 --protect`；期间中断怎么办（Ctrl-C 后跑 `cleanup --run-id`）。
6. 启用可选 backend：ripeatlas（申请 key 与 credits 的链接 `https://atlas.ripe.net/docs/getting-started/credits`）、itdog（`--enable-backend itdog`，风险说明）。
7. 清理：`cleanup --run-id`；`--include-infra` 的前提。
8. 故障排查表：SSM 未 online（检查 instance profile、AL2023 AMI、子网出网）；`InvalidParameterValue` IAM 传播；配额不足；ip-ranges 下载失败；所有候选 `reverse_unreachable`（目标被封或 ICMP 限速，换目标）。
9. 把 winner 交给生产：不要 stop；建议只跑 Nginx/HAProxy/代理，业务放后端；如何删除 winner 标签或解除 protection。

- [ ] **Step 7: 全量测试与提交**

```bash
pytest -q
git add -A && git commit -m "docs: README, MANUAL, and one-command wrapper script"
```

---

## 自查记录（计划作者填写）

**Spec 覆盖：** §2 流程 → Task 10/15；§3 结构 → 文件结构表；§4 配置 → Task 2；§5 模型 → Task 1；§6 打分 → Task 9；§7 reverse → Task 8；§8.1/8.2/8.3 → Task 12/13/14；§9 基础设施与 winner → Task 5/6/10/15；§10 报告 → Task 11；§11 错误处理 → Task 7/8/10/15（配额不足时 `run_instances` 的 MaxCount=n/MinCount=1 自动缩小 batch）；§12 测试 → 每任务的测试文件；§13 阶段 → 任务顺序。

**与 spec 的偏差：** §5 数据模型在 `IspProbe` 上多了 `target`、`method` 字段，`Candidate` 多了 `ssm_online`，用于报告和 reverse 跳过离线机；均为增补，不改变语义。

**类型一致性：** `ProbeBackend.probe -> dict[public_ip, ProbeResult]`、`run_backends -> (dict[ip, list[ProbeResult]], dict)`、`score_candidate(candidate, reputation, probe_results, weights, min_backends, reverse_enabled)`、`Ec2Manager.launch(n, run_id, round_no, infra, instance_type)`、`SsmRunner.run_script -> (status, output)` 在 Task 6/7/8/9/10/15 中签名一致。
