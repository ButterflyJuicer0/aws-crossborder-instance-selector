# 本地 Web 向导 实施计划

> 历史设计记录（2026-09-09 已归档）：本文保留初始方案，部分行为已调整。当前配置、资源生命周期和测量限制以 [README](../../../README.md) 和 [MANUAL](../../../MANUAL.md) 为准。

> 下列步骤和代码片段是初始实施计划的历史快照，不作为当前实现或操作指令。

**Goal:** 在现有 `crossborder_selector` 之上加一个本地 Web 向导：六步引导客户完成环境检查、参数配置、计划确认、实时运行、查看报告、选定机器，并提供不接触 AWS 的演示模式。

**Architecture:** 新子包 `crossborder_selector/web/`。`server.py` 只做 HTTP 编解码与路由；`api.py` 实现各接口业务并通过可注入的 boto3 factory 访问 AWS；`runs.py` 的 `RunManager` 在后台线程运行现有 `Orchestrator`，通过新增的 `on_event` / `should_stop` 钩子产生事件并落盘；`static/index.html` 是单页向导前端；`demo.py` 提供模拟 Orchestrator。核心模块唯一改动是给 `Orchestrator` 加两个可选钩子。

**Tech Stack:** Python 3.11 标准库（`http.server`、`threading`、`queue`、`json`）、boto3（已有）、原生 HTML/CSS/JS。不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-08-web-wizard-design.md`（含 §10 演示模式）

## Global Constraints

- 仓库根：`/Users/jinhaoz/Documents/TLI/Solution/aws-crossborder-instance-selector`；venv 用 `. .venv/bin/activate`。
- 不新增第三方依赖；`requirements.txt` 不改。
- 服务只绑定 `127.0.0.1`；默认端口 `8765`。
- 所有 API 错误响应为 `{"error": "<中文说明>"}`；成功响应为 JSON。SSE 事件为 `data: <json>\n\n`，字段 `type`、`ts` 必有。
- 事件类型固定：`log`、`round_started`、`candidates`、`vetoed`、`round_done`、`finished`、`failed`、`cancelled`。
- run 状态固定：`running`、`finished`、`failed`、`cancelled`、`unknown`。
- 持久化路径：`out/<run-id>/status.json`、`out/<run-id>/events.jsonl`；报告仍由 `report.write_reports` 写到 `out/<run-id>/`。
- 现有 101 个测试必须持续通过；`Orchestrator` 改动向后兼容（新参数有默认值）。
- 注释与界面文字中文，标识符英文；English conventional-commit messages；不用非包容性词汇；不用 emoji 作为界面标记。
- 界面文案遵循 `/Users/jinhaoz/.claude/skills/standardizing-zh-terminology/SKILL.md`：直接陈述，不用比喻和修辞对比。
- 每个任务结束提交一次。

---

## 文件结构

| 路径 | 职责 |
|---|---|
| `crossborder_selector/orchestrator.py`（修改） | 新增 `on_event`、`should_stop` 钩子 |
| `crossborder_selector/web/__init__.py` | 空 |
| `crossborder_selector/web/pricing.py` | 机型候选表与估算价、成本公式 |
| `crossborder_selector/web/api.py` | `Api` 类：env / options / plan / select / cleanup 的业务实现 |
| `crossborder_selector/web/runs.py` | `RunManager`、`RunRecord`、事件落盘、状态恢复 |
| `crossborder_selector/web/demo.py` | `DemoOrchestrator` 与 demo factory |
| `crossborder_selector/web/server.py` | `Handler`、路由表、SSE、`serve(host, port, api, runs)` |
| `crossborder_selector/web/__main__.py` | 参数解析：`--port`、`--output-dir`、`--demo`、`--no-browser` |
| `crossborder_selector/web/static/index.html` | 单页向导 |
| `scripts/start_web.sh` | 启动脚本 |
| `tests/test_orchestrator.py`（追加） | 钩子测试 |
| `tests/test_web_pricing.py`、`test_web_api.py`、`test_web_runs.py`、`test_web_server.py`、`test_web_demo.py` | 新测试 |
| `README.md`、`MANUAL.md`、`TESTING.md`、`.claude/skills/crossborder-select/SKILL.md`（修改） | 文档 |

---

### Task 1: Orchestrator 钩子（on_event / should_stop）

**Files:**
- Modify: `crossborder_selector/orchestrator.py`
- Test: `tests/test_orchestrator.py`（追加）

**Interfaces:**
- Produces: `Orchestrator(..., clock=utc_now_iso, log=print, on_event=None, should_stop=None)`。`on_event(dict)` 在以下位置被调用：`round_started {round, batch_size}`、`candidates {round, items:[{instance_id, public_ip, prefix}]}`、`vetoed {round, items:[{instance_id, public_ip, reason}]}`、`round_done {round, kept:[{instance_id, public_ip, composite}], terminated:[ids], backend_errors:{}}`。每个 dict 都带 `type` 与 `ts`（`self.now()`）。`should_stop()` 在每轮 launch 前检查，为真则 `stop_reason="cancelled"`，跳出循环，winner 照常标记。`RunResult.stop_reason` 新增合法值 `cancelled`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_orchestrator.py` 末尾）

```python
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
        b = ScriptedBackend({"10.0.0.1": 100.0, "10.0.0.2": 150.0})
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_orchestrator.py -q -k "on_event or should_stop"`
Expected: FAIL，`TypeError: unexpected keyword argument 'on_event'`

- [ ] **Step 3: 实现**

在 `__init__` 增加参数并保存：

```python
    def __init__(self, cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id,
                 clock=utc_now_iso, log=print, on_event=None, should_stop=None):
        ...
        self.on_event = on_event or (lambda e: None)
        self.should_stop = should_stop or (lambda: False)

    def _emit(self, type_, **fields):
        self.on_event({"type": type_, "ts": self.now(), **fields})
```

在 `run()` 的 for 循环开头（`self.log(f"[round {rno}] launching ...")` 之前）加：

```python
                if self.should_stop():
                    stop = "cancelled"
                    break
                self._emit("round_started", round=rno, batch_size=self.cfg.batch_size)
```

在 `_round` 中：构造 `cands` 之后加
`self._emit("candidates", round=rno, items=[{"instance_id": c.instance_id, "public_ip": c.public_ip, "prefix": c.prefix} for c in cands])`；
终止 vetoed 之后加
`self._emit("vetoed", round=rno, items=[{"instance_id": v.candidate.instance_id, "public_ip": v.candidate.public_ip, "reason": v.veto_reason} for v in vetoed])`；
`return RoundResult(...)` 之前加
`self._emit("round_done", round=rno, kept=[{"instance_id": s.candidate.instance_id, "public_ip": s.candidate.public_ip, "composite": s.composite} for s in kept], terminated=list(terminated), backend_errors=dict(errors))`。

同时更新 `models.py` 中 `RunResult.stop_reason` 的注释，加入 `cancelled`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest -q`
Expected: 103 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: orchestrator on_event and should_stop hooks"
```

---

### Task 2: pricing 与 Api（env / options / plan）

**Files:**
- Create: `crossborder_selector/web/__init__.py`、`crossborder_selector/web/pricing.py`、`crossborder_selector/web/api.py`
- Test: `tests/test_web_pricing.py`、`tests/test_web_api.py`

**Interfaces:**
- `pricing.py`：`INSTANCE_CATALOG: list[dict]`，每项 `{"type","vcpu","memory_gib","arch","hourly_usd"}`，包含 t3.nano(2,0.5,x86_64,0.0066)、t3.micro(2,1,x86_64,0.0132)、t3.small(2,2,x86_64,0.0264)、t3.medium(2,4,x86_64,0.0528)、t4g.nano(2,0.5,arm64,0.0053)、t4g.micro(2,1,arm64,0.0106)、t4g.small(2,2,arm64,0.0212)、m6g.medium(1,4,arm64,0.0495)、c6g.medium(1,2,arm64,0.0435)；`IPV4_HOURLY_USD = 0.005`；`ROUND_MINUTES = 8`；`hourly_for(instance_type) -> float`（未知机型返回 `0.02`）；`estimate(instance_type, batch_size, max_rounds) -> dict{"estimated_cost_usd": round(batch*rounds*(hourly+0.005)*8/60, 3), "estimated_minutes": rounds*8, "note": "估算值，实际以账单为准"}`；`REGIONS: list[dict]` 至少含 ap-east-1 香港、ap-northeast-1 东京、ap-southeast-1 新加坡、ap-northeast-2 首尔、us-west-2 俄勒冈、us-west-1 加州，每项 `{"code","name"}`。
- `api.py`：`class Api(factory=default_factory, config_path=None, cwd=None)`。方法：`env(region) -> dict`；`options(region) -> dict`；`plan(overrides: dict) -> dict`；`load(overrides) -> Config`（内部：`load_config(config_path or ("config.yaml" if exists in cwd else None), overrides)`，并把 `region` 覆盖）；`select(run_id, instance_id, protect, terminate_others, region) -> dict`；`cleanup(run_id, region) -> dict`。`class ApiError(Exception)` 带 `status` 与 `message`。`redact(dict) -> dict` 删除键 `api_key`、`api_token`、`abuseipdb_api_key`。

- [ ] **Step 1: 写失败测试**

`tests/test_web_pricing.py`：

```python
from crossborder_selector.web.pricing import INSTANCE_CATALOG, hourly_for, estimate, REGIONS, IPV4_HOURLY_USD


def test_catalog_has_expected_types():
    types = {i["type"] for i in INSTANCE_CATALOG}
    assert {"t3.nano", "t4g.nano", "t3.medium", "m6g.medium", "c6g.medium"} <= types
    assert all({"type", "vcpu", "memory_gib", "arch", "hourly_usd"} <= set(i) for i in INSTANCE_CATALOG)


def test_hourly_and_estimate():
    assert hourly_for("t3.nano") == 0.0066 and hourly_for("zz.huge") == 0.02
    e = estimate("t3.nano", 20, 3)
    assert e["estimated_minutes"] == 24
    assert e["estimated_cost_usd"] == round(20 * 3 * (0.0066 + IPV4_HOURLY_USD) * 8 / 60, 3)
    assert "估算" in e["note"]


def test_regions_include_hk_first():
    assert REGIONS[0]["code"] == "ap-east-1" and any(r["code"] == "ap-northeast-1" for r in REGIONS)
```

`tests/test_web_api.py`：

```python
import pytest
from botocore.exceptions import ClientError

from crossborder_selector.web.api import Api, ApiError, redact


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeSts:
    def get_caller_identity(self): return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}


class FakeQuotas:
    def __init__(self, value=64.0, fail=False): self.value, self.fail = value, fail
    def get_service_quota(self, ServiceCode, QuotaCode):
        if self.fail: raise _err("AccessDeniedException")
        assert (ServiceCode, QuotaCode) == ("ec2", "L-1216C47A")
        return {"Quota": {"Value": self.value}}


class FakeEc2:
    def __init__(self, default_vpc=True, offerings=("t3.nano", "t3.micro", "t4g.nano"), winners=()):
        self.default_vpc, self.offerings, self.winners = default_vpc, offerings, winners
    def describe_vpcs(self, Filters):
        return {"Vpcs": [{"VpcId": "vpc-1"}] if self.default_vpc else []}
    def describe_subnets(self, Filters):
        return {"Subnets": [{"SubnetId": "s-1"}, {"SubnetId": "s-2"}]}
    def describe_instances(self, Filters):
        for f in Filters:
            if f["Name"] == "tag:crossborder-winner":
                return {"Reservations": [{"Instances": [{"InstanceId": w, "PublicIpAddress": "1.1.1.1", "InstanceType": "t3.nano",
                                                          "Tags": [{"Key": "crossborder-score", "Value": "91.2"}]} for w in self.winners]}]}
        return {"Reservations": [{"Instances": [{"InstanceId": "i-r1"}, {"InstanceId": "i-r2"}]}]}
    def describe_instance_type_offerings(self, LocationType, Filters):
        return {"InstanceTypeOfferings": [{"InstanceType": t} for t in self.offerings]}


def _factory(**kw):
    clients = {"sts": FakeSts(), "ec2": FakeEc2(**kw.get("ec2", {})), "service-quotas": FakeQuotas(**kw.get("quotas", {})),
               "iam": object(), "ssm": object()}
    return lambda cfg: clients


def test_env_ok(tmp_path):
    api = Api(factory=_factory(ec2={"winners": ["i-w1"]}), cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["caller"]["account"] == "123456789012" and e["default_vpc"] == {"present": True, "subnets": 2}
    assert e["vcpu_quota"] == 64.0 and e["running_instances"] == 2
    assert e["winners"][0]["instance_id"] == "i-w1" and e["winners"][0]["score"] == "91.2"
    assert e["config_yaml_present"] is False and e["ok"] is True


def test_env_no_default_vpc_and_quota_denied(tmp_path):
    (tmp_path / "config.yaml").write_text("region: ap-east-1\n")
    api = Api(factory=_factory(ec2={"default_vpc": False}, quotas={"fail": True}), cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["default_vpc"]["present"] is False and e["vcpu_quota"] is None
    assert e["config_yaml_present"] is True and e["ok"] is False and "默认 VPC" in e["problems"][0]


def test_env_sts_failure_is_reported_not_raised(tmp_path):
    class BadSts:
        def get_caller_identity(self): raise _err("ExpiredToken")
    f = _factory(); clients = f(None); clients["sts"] = BadSts()
    api = Api(factory=lambda cfg: clients, cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["ok"] is False and e["caller"] is None and any("凭证" in p for p in e["problems"])


def test_options_filters_catalog_by_offerings(tmp_path):
    api = Api(factory=_factory(ec2={"offerings": ("t3.nano", "t4g.nano")}), cwd=str(tmp_path))
    o = api.options("ap-east-1")
    assert [i["type"] for i in o["instance_types"]] == ["t3.nano", "t4g.nano"]
    assert o["regions"][0]["code"] == "ap-east-1"
    names = {b["name"] for b in o["backends"]}
    assert names == {"reverse", "globalping", "ripeatlas", "itdog"}
    rip = next(b for b in o["backends"] if b["name"] == "ripeatlas")
    assert rip["needs_key"] is True and rip["key_present"] is False
    assert o["defaults"]["batch_size"] == 10 and "abuseipdb_api_key" not in str(o["defaults"])


def test_options_falls_back_to_full_catalog_when_offerings_fail(tmp_path):
    class NoOffer(FakeEc2):
        def describe_instance_type_offerings(self, **kw): raise _err("UnauthorizedOperation")
    f = _factory(); clients = f(None); clients["ec2"] = NoOffer()
    o = Api(factory=lambda cfg: clients, cwd=str(tmp_path)).options("ap-east-1")
    assert len(o["instance_types"]) >= 9 and o["instance_types_source"] == "catalog"


def test_plan_summary_and_cost(tmp_path):
    api = Api(factory=_factory(), cwd=str(tmp_path))
    p = api.plan({"region": "ap-east-1", "batch_size": 4, "max_rounds": 2, "instance_type": "t3.nano"})
    assert "4 x t3.nano" in p["plan_summary"] and p["estimated_minutes"] == 16
    assert p["estimated_cost_usd"] == round(4 * 2 * (0.0066 + 0.005) * 8 / 60, 3)
    assert p["config"]["batch_size"] == 4 and "api_key" not in str(p["config"])


def test_plan_rejects_bad_override(tmp_path):
    with pytest.raises(ApiError) as ei:
        Api(factory=_factory(), cwd=str(tmp_path)).plan({"batch_size": 0})
    assert ei.value.status == 400


def test_redact_drops_secret_keys_recursively():
    assert redact({"a": {"api_key": "x", "b": 1}, "api_token": "y"}) == {"a": {"b": 1}}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_web_pricing.py tests/test_web_api.py -q`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 pricing.py**

```python
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
```

- [ ] **Step 4: 实现 api.py**

```python
"""Web 接口的业务实现。只依赖可注入的 boto3 factory，便于测试。"""
import os
from dataclasses import asdict

from botocore.exceptions import BotoCoreError, ClientError

from crossborder_selector.aws.ec2 import Ec2Manager, WINNER_TAG
from crossborder_selector.cli import default_factory, plan_summary
from crossborder_selector.config import load_config, KNOWN_BACKENDS
from crossborder_selector.web import pricing

_SECRET_KEYS = {"api_key", "api_token", "abuseipdb_api_key"}
_BACKEND_DOC = {
    "reverse": {"needs_key": False, "desc": "候选机经 SSM 向大陆三网目标 ping + tcping"},
    "globalping": {"needs_key": False, "desc": "HK/TW 公共探针探测候选 IP"},
    "ripeatlas": {"needs_key": True, "desc": "RIPE Atlas 大陆在线探针，需要 api_key"},
    "itdog": {"needs_key": False, "desc": "itdog.cn 三网家宽节点，非官方接口"},
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def redact(obj):
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _client_factory_with_quotas(factory):
    """CLI 的 factory 只给 ec2/iam/ssm；这里补上 sts 与 service-quotas。"""
    def inner(cfg):
        clients = dict(factory(cfg))
        if "sts" not in clients or "service-quotas" not in clients:
            import boto3
            clients.setdefault("sts", boto3.client("sts", region_name=cfg.region))
            clients.setdefault("service-quotas", boto3.client("service-quotas", region_name=cfg.region))
        return clients
    return inner


class Api:
    def __init__(self, factory=default_factory, config_path=None, cwd=None):
        self.factory = _client_factory_with_quotas(factory)
        self.config_path, self.cwd = config_path, cwd or os.getcwd()

    # ---------- 配置 ----------
    def _config_file(self):
        if self.config_path:
            return self.config_path
        p = os.path.join(self.cwd, "config.yaml")
        return p if os.path.exists(p) else None

    def load(self, overrides: dict):
        try:
            return load_config(self._config_file(), dict(overrides or {}))
        except (ValueError, TypeError) as e:
            raise ApiError(400, f"配置无效：{e}")

    # ---------- ① 环境检查 ----------
    def env(self, region: str) -> dict:
        cfg = self.load({"region": region})
        clients = self.factory(cfg)
        out = {"region": region, "caller": None, "default_vpc": {"present": False, "subnets": 0}, "vcpu_quota": None,
               "running_instances": None, "winners": [], "config_yaml_present": self._config_file() is not None,
               "problems": []}
        try:
            ident = clients["sts"].get_caller_identity()
            out["caller"] = {"account": ident["Account"], "arn": ident["Arn"]}
        except (ClientError, BotoCoreError, KeyError) as e:
            out["problems"].append(f"AWS 凭证不可用：{e}。请先执行 aws configure 或设置环境变量。")
            out["ok"] = False
            return out
        ec2 = clients["ec2"]
        try:
            vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
            if vpcs:
                subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]}])["Subnets"]
                out["default_vpc"] = {"present": True, "subnets": len(subnets)}
            elif not (cfg.subnet_id and cfg.security_group_id):
                out["problems"].append(f"Region {region} 没有默认 VPC。请在 config.yaml 填写 subnet_id 与 security_group_id。")
        except (ClientError, BotoCoreError) as e:
            out["problems"].append(f"无法查询 VPC：{e}")
        try:
            out["vcpu_quota"] = float(clients["service-quotas"].get_service_quota(
                ServiceCode="ec2", QuotaCode="L-1216C47A")["Quota"]["Value"])
        except (ClientError, BotoCoreError, KeyError):
            out["vcpu_quota"] = None
        try:
            r = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}])
            out["running_instances"] = sum(len(res["Instances"]) for res in r["Reservations"])
        except (ClientError, BotoCoreError):
            out["running_instances"] = None
        try:
            r = ec2.describe_instances(Filters=[{"Name": f"tag:{WINNER_TAG}", "Values": ["true"]},
                                                {"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]}])
            for res in r["Reservations"]:
                for i in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    out["winners"].append({"instance_id": i["InstanceId"], "public_ip": i.get("PublicIpAddress", ""),
                                           "instance_type": i.get("InstanceType", ""), "score": tags.get("crossborder-score", "")})
        except (ClientError, BotoCoreError):
            pass
        out["ok"] = not out["problems"]
        return out

    # ---------- ② 可选项 ----------
    def options(self, region: str) -> dict:
        cfg = self.load({"region": region})
        clients = self.factory(cfg)
        source, allowed = "catalog", None
        try:
            r = clients["ec2"].describe_instance_type_offerings(
                LocationType="region", Filters=[{"Name": "instance-type", "Values": [i["type"] for i in pricing.INSTANCE_CATALOG]}])
            allowed = {o["InstanceType"] for o in r["InstanceTypeOfferings"]}
            source = "offerings"
        except (ClientError, BotoCoreError, KeyError):
            allowed = None
        types = [i for i in pricing.INSTANCE_CATALOG if allowed is None or i["type"] in allowed]
        backends = []
        for name in KNOWN_BACKENDS:
            b = cfg.backends[name]
            key_present = bool((b.get("api_key") or "").strip()) if _BACKEND_DOC[name]["needs_key"] else True
            backends.append({"name": name, "enabled": b["enabled"], "needs_key": _BACKEND_DOC[name]["needs_key"],
                             "key_present": key_present, "desc": _BACKEND_DOC[name]["desc"]})
        return {"regions": pricing.REGIONS, "instance_types": types, "instance_types_source": source,
                "backends": backends, "defaults": redact(asdict(cfg)), "ipv4_hourly_usd": pricing.IPV4_HOURLY_USD}

    # ---------- ③ 计划 ----------
    def plan(self, overrides: dict) -> dict:
        cfg = self.load(overrides)
        est = pricing.estimate(cfg.instance_type, cfg.batch_size, cfg.max_rounds)
        return {"plan_summary": plan_summary(cfg, "<run-id 将在开始时生成>"), **est, "config": redact(asdict(cfg))}

    # ---------- ⑤/⑥ 选定与清理 ----------
    def select(self, run_id: str, instance_id: str, protect: bool, terminate_others: bool, region: str,
               winner_ids: list) -> dict:
        if instance_id not in winner_ids:
            raise ApiError(400, "该实例不是本次 run 保留的候选，不能选定。")
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        if protect:
            m.protect(instance_id)
        others = [w for w in winner_ids if w != instance_id]
        terminated = []
        if terminate_others and others:
            m.terminate(others)
            terminated = others
        return {"selected": instance_id, "protected": bool(protect), "terminated": terminated}

    def cleanup(self, run_id: str, region: str) -> dict:
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        ids = m.list_run_instances(run_id)
        m.terminate(ids)
        return {"run_id": run_id, "terminated": ids}
```

注意：`select` 的 `winner_ids` 由 `RunManager` 从报告中读出后传入（Task 3/4 负责），`Api` 本身不读文件。`env` 的测试注入 factory 已包含 `sts` 与 `service-quotas`，所以 `_client_factory_with_quotas` 不会触发 boto3 导入。

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_web_pricing.py tests/test_web_api.py -q && pytest -q`
Expected: 新测试 11 passed；全量 114 passed

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat: web api (env/options/plan/select/cleanup) and pricing catalog"
```

---

### Task 3: RunManager（后台运行、事件、持久化）

**Files:**
- Create: `crossborder_selector/web/runs.py`
- Test: `tests/test_web_runs.py`

**Interfaces:**
- `class RunRecord`：字段 `run_id, state, region, started_at, finished_at, config (redacted dict), events (list[dict]), stop_reason, winners (list[dict]), report_paths (dict), leftover_instance_ids (list), error (str)`；方法 `to_summary() -> dict`（不含 events）、`to_detail() -> dict`（events 最近 200 条）。
- `class RunManager(api: Api, output_dir: str, orchestrator_factory=None, clock=utc_now_iso)`：
  - `start(overrides: dict) -> str`：生成 run-id（`cli.new_run_id`），写 `status.json`（state=running），起守护线程执行 `_run(record, cfg)`，返回 run-id。
  - `_run(record, cfg)`：`clients = api.factory(cfg)`；`infra = ensure_infra(...)`；`ssm_runner = SsmRunner(clients["ssm"])`；`backends = build_backends(cfg, ssm_runner)`；`prefixes = load_ip_ranges(cache_path=os.path.join(output_dir, "ip-ranges.json"))`；`orch = orchestrator_factory(cfg, ec2, ssm, backends, sources, prefix_lookup, infra, run_id, log=..., on_event=..., should_stop=...)`（默认 `Orchestrator`）；`result = orch.run()`；`paths = write_reports(result, cfg, out_dir=output_dir)`；发 `finished`；state=`cancelled` 若 `stop_reason=="cancelled"` 否则 `finished`。异常：发 `failed {message, leftover_instance_ids}`（`Ec2Manager(clients["ec2"]).list_run_instances(run_id)`，失败则 `[]`），state=`failed`。
  - `emit(run_id, event)`：追加到 `record.events`、写 `events.jsonl`、推给所有订阅队列。`log` 回调包装为 `{"type":"log","message":...}`。
  - `subscribe(run_id) -> queue.Queue`、`unsubscribe(run_id, q)`。
  - `cancel(run_id)`：设置 `threading.Event`；`should_stop` 返回其 `is_set()`；同时发 `{"type":"cancelled"}` 事件（表示已请求取消）。
  - `get(run_id) -> RunRecord | None`；`list() -> list[dict]`（内存记录 + 扫描 `output_dir/*/status.json`；文件里 state=running 但内存里没有的标为 `unknown`）。
  - `winner_ids(run_id) -> list[str]`：优先内存 `record.winners`，否则读 `out/<run-id>/report.json` 的 `winners[*].instance_id`。
  - `_persist(record)`：写 `status.json`（`to_summary()`）。
- 事件字典缺 `ts` 时由 `emit` 补 `clock()`。

- [ ] **Step 1: 写失败测试**

`tests/test_web_runs.py`：

```python
import json
import os
import queue
import threading
import time

from crossborder_selector.models import Candidate, CandidateScore, RoundResult, RunResult
from crossborder_selector.web.runs import RunManager


class FakeApi:
    """只提供 factory 与 load；不触碰 AWS。"""
    def __init__(self, cfg):
        self._cfg = cfg
        self.factory = lambda c: {"ec2": FakeEc2(), "iam": object(), "ssm": object()}
    def load(self, overrides):
        return self._cfg


class FakeEc2:
    def list_run_instances(self, run_id): return ["i-left"]


def _score(iid, ip, comp):
    return CandidateScore(Candidate(iid, ip, "18.162.0.0/16", 1, "t"), None, [], {}, {}, comp, True, "")


class FakeOrch:
    """模拟一次两轮运行，发出真实事件序列。"""
    behaviour = "ok"
    def __init__(self, cfg, ec2, ssm, backends, sources, prefix_lookup, infra, run_id, log, on_event, should_stop, **kw):
        self.run_id, self.log, self.on_event, self.should_stop, self.cfg = run_id, log, on_event, should_stop, cfg
    def run(self):
        if FakeOrch.behaviour == "boom":
            raise RuntimeError("ssm exploded")
        self.log("[round 1] launching")
        self.on_event({"type": "round_started", "round": 1, "batch_size": 2})
        self.on_event({"type": "round_done", "round": 1, "kept": [{"instance_id": "i-1", "public_ip": "10.0.0.1", "composite": 88.0}], "terminated": ["i-2"], "backend_errors": {}})
        stop = "cancelled" if self.should_stop() else "max_rounds"
        w = _score("i-1", "10.0.0.1", 88.0)
        return RunResult(self.run_id, self.cfg.region, [RoundResult(1, [w.candidate], [], [w], [w], ["i-2"], {})], [w], "t0", "t1", stop)


def _patched(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr("crossborder_selector.web.runs.ensure_infra", lambda ec2, iam, ssm, c: object())
    monkeypatch.setattr("crossborder_selector.web.runs.build_backends", lambda c, s: [])
    monkeypatch.setattr("crossborder_selector.web.runs.build_sources", lambda c: [])
    monkeypatch.setattr("crossborder_selector.web.runs.load_ip_ranges", lambda cache_path=None: [])
    monkeypatch.setattr("crossborder_selector.web.runs.SsmRunner", lambda client: object())
    return RunManager(FakeApi(cfg), str(tmp_path / "out"), orchestrator_factory=FakeOrch)


def _wait(mgr, run_id, states=("finished", "failed", "cancelled"), timeout=5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if mgr.get(run_id).state in states:
            return mgr.get(run_id)
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_start_runs_and_persists(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    q = None
    run_id = mgr.start({"region": "ap-east-1"})
    assert run_id.startswith("xb-")
    rec = _wait(mgr, run_id)
    assert rec.state == "finished" and rec.stop_reason == "max_rounds"
    types = [e["type"] for e in rec.events]
    assert types == ["log", "round_started", "round_done", "finished"]
    assert all("ts" in e for e in rec.events)
    assert rec.winners[0]["instance_id"] == "i-1" and rec.report_paths["json"].endswith("report.json")
    assert os.path.exists(rec.report_paths["json"])
    st = json.load(open(tmp_path / "out" / run_id / "status.json"))
    assert st["state"] == "finished" and "events" not in st
    lines = open(tmp_path / "out" / run_id / "events.jsonl").read().splitlines()
    assert len(lines) == 4 and json.loads(lines[-1])["type"] == "finished"
    assert mgr.winner_ids(run_id) == ["i-1"]
    assert "api_key" not in json.dumps(rec.config)


def test_subscribe_receives_live_events(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    # 先订阅再启动：用 start 前注册的 run_id 不可知，故通过 orchestrator 里的 should_stop 阻塞不可行；改为启动后立刻订阅并校验回放+实时
    run_id = mgr.start({"region": "ap-east-1"})
    q = mgr.subscribe(run_id)
    got = []
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            ev = q.get(timeout=0.2)
        except queue.Empty:
            continue
        got.append(ev["type"])
        if ev["type"] in ("finished", "failed"):
            break
    assert got[-1] == "finished"
    mgr.unsubscribe(run_id, q)


def test_failure_records_leftovers(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "boom"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    run_id = mgr.start({"region": "ap-east-1"})
    rec = _wait(mgr, run_id)
    assert rec.state == "failed" and "ssm exploded" in rec.error
    assert rec.events[-1]["type"] == "failed" and rec.events[-1]["leftover_instance_ids"] == ["i-left"]
    FakeOrch.behaviour = "ok"


def test_cancel_sets_flag_and_state(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    FakeOrch.behaviour = "ok"
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    gate = threading.Event()
    class SlowOrch(FakeOrch):
        def run(self):
            gate.wait(5)
            return super().run()
    mgr.orchestrator_factory = SlowOrch
    run_id = mgr.start({"region": "ap-east-1"})
    mgr.cancel(run_id)
    gate.set()
    rec = _wait(mgr, run_id)
    assert rec.state == "cancelled" and rec.stop_reason == "cancelled"
    assert "cancelled" in [e["type"] for e in rec.events]


def test_list_marks_stale_running_as_unknown(monkeypatch, tmp_path):
    from crossborder_selector.config import load_config
    out = tmp_path / "out" / "xb-old"
    out.mkdir(parents=True)
    (out / "status.json").write_text(json.dumps({"run_id": "xb-old", "state": "running", "region": "ap-east-1", "started_at": "t"}))
    mgr = _patched(monkeypatch, tmp_path, load_config(None, {"region": "ap-east-1"}))
    rows = mgr.list()
    assert rows[0]["run_id"] == "xb-old" and rows[0]["state"] == "unknown"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_web_runs.py -q`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 runs.py**

```python
"""后台运行管理：线程、事件队列、状态与事件落盘。"""
import glob
import json
import os
import queue
import threading
from dataclasses import dataclass, field, asdict

from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import ensure_infra
from crossborder_selector.aws.ipranges import load_ip_ranges, PrefixLookup
from crossborder_selector.aws.ssm import SsmRunner
from crossborder_selector.cli import build_backends, new_run_id
from crossborder_selector.orchestrator import Orchestrator, utc_now_iso
from crossborder_selector.report import write_reports
from crossborder_selector.reputation.abuseipdb import build_sources
from crossborder_selector.web.api import redact

MAX_DETAIL_EVENTS = 200


@dataclass
class RunRecord:
    run_id: str
    state: str
    region: str
    started_at: str
    config: dict
    finished_at: str = ""
    events: list = field(default_factory=list)
    stop_reason: str = ""
    winners: list = field(default_factory=list)
    report_paths: dict = field(default_factory=dict)
    leftover_instance_ids: list = field(default_factory=list)
    error: str = ""

    def to_summary(self) -> dict:
        d = asdict(self)
        d.pop("events")
        return d

    def to_detail(self) -> dict:
        d = asdict(self)
        d["events"] = self.events[-MAX_DETAIL_EVENTS:]
        return d


class RunManager:
    def __init__(self, api, output_dir, orchestrator_factory=None, clock=utc_now_iso):
        self.api, self.output_dir, self.clock = api, output_dir, clock
        self.orchestrator_factory = orchestrator_factory or Orchestrator
        self._records, self._subs, self._flags = {}, {}, {}
        self._lock = threading.Lock()
        os.makedirs(output_dir, exist_ok=True)

    # ---------- 查询 ----------
    def get(self, run_id):
        return self._records.get(run_id)

    def list(self) -> list:
        rows = {}
        for path in glob.glob(os.path.join(self.output_dir, "*", "status.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if d.get("state") == "running" and d.get("run_id") not in self._records:
                d["state"] = "unknown"
            rows[d.get("run_id", os.path.basename(os.path.dirname(path)))] = d
        for rid, rec in self._records.items():
            rows[rid] = rec.to_summary()
        return sorted(rows.values(), key=lambda d: d.get("started_at", ""), reverse=True)

    def winner_ids(self, run_id) -> list:
        rec = self._records.get(run_id)
        if rec and rec.winners:
            return [w["instance_id"] for w in rec.winners]
        path = os.path.join(self.output_dir, run_id, "report.json")
        try:
            with open(path) as f:
                return [w["instance_id"] for w in json.load(f).get("winners", [])]
        except (OSError, ValueError):
            return []

    # ---------- 事件 ----------
    def subscribe(self, run_id) -> "queue.Queue":
        q = queue.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id, q):
        with self._lock:
            if q in self._subs.get(run_id, []):
                self._subs[run_id].remove(q)

    def emit(self, run_id, event: dict):
        event = dict(event)
        event.setdefault("ts", self.clock())
        rec = self._records[run_id]
        with self._lock:
            rec.events.append(event)
            subs = list(self._subs.get(run_id, []))
        with open(os.path.join(self.output_dir, run_id, "events.jsonl"), "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        for q in subs:
            q.put(event)

    def _persist(self, rec: RunRecord):
        d = os.path.join(self.output_dir, rec.run_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(rec.to_summary(), f, ensure_ascii=False, indent=2)

    # ---------- 启动 / 取消 ----------
    def start(self, overrides: dict) -> str:
        cfg = self.api.load(overrides)
        run_id = new_run_id()
        rec = RunRecord(run_id, "running", cfg.region, self.clock(), redact(asdict(cfg)))
        self._records[run_id] = rec
        self._flags[run_id] = threading.Event()
        self._persist(rec)
        threading.Thread(target=self._run, args=(rec, cfg), daemon=True, name=f"run-{run_id}").start()
        return run_id

    def cancel(self, run_id):
        flag = self._flags.get(run_id)
        if flag is None:
            return False
        flag.set()
        self.emit(run_id, {"type": "cancelled", "message": "已请求取消，当前轮结束后停止并保留在位 winner"})
        return True

    def _run(self, rec: RunRecord, cfg):
        run_id, clients = rec.run_id, None
        try:
            clients = self.api.factory(cfg)
            infra = ensure_infra(clients["ec2"], clients["iam"], clients["ssm"], cfg)
            ssm_runner = SsmRunner(clients["ssm"])
            prefixes = load_ip_ranges(cache_path=os.path.join(self.output_dir, "ip-ranges.json"))
            orch = self.orchestrator_factory(
                cfg, Ec2Manager(clients["ec2"]), ssm_runner, build_backends(cfg, ssm_runner), build_sources(cfg.reputation),
                PrefixLookup(prefixes, cfg.region), infra, run_id,
                log=lambda m: self.emit(run_id, {"type": "log", "message": str(m)}),
                on_event=lambda e: self.emit(run_id, e),
                should_stop=self._flags[run_id].is_set)
            result = orch.run()
            paths = write_reports(result, cfg, out_dir=self.output_dir)
            rec.stop_reason = result.stop_reason
            rec.winners = [{"instance_id": w.candidate.instance_id, "public_ip": w.candidate.public_ip,
                            "prefix": w.candidate.prefix, "composite": w.composite, "round": w.candidate.round,
                            "isp_scores": w.isp_scores} for w in result.winners]
            rec.report_paths = {k: v for k, v in paths.items() if k in ("json", "md", "csv")}
            rec.state = "cancelled" if result.stop_reason == "cancelled" else "finished"
            rec.finished_at = self.clock()
            self._persist(rec)
            self.emit(run_id, {"type": "finished", "stop_reason": rec.stop_reason, "winners": rec.winners,
                               "report_paths": rec.report_paths})
        except BaseException as e:  # 线程内任何失败都要落状态并带上遗留实例
            rec.state, rec.error, rec.finished_at = "failed", f"{type(e).__name__}: {e}", self.clock()
            try:
                rec.leftover_instance_ids = Ec2Manager(clients["ec2"]).list_run_instances(run_id) if clients else []
            except Exception:
                rec.leftover_instance_ids = []
            self._persist(rec)
            self.emit(run_id, {"type": "failed", "message": rec.error, "leftover_instance_ids": rec.leftover_instance_ids})
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_web_runs.py -q && pytest -q`
Expected: 5 passed；全量 119 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: RunManager with background runs, SSE queues and persistence"
```

---

### Task 4: 演示模式（DemoOrchestrator 与 demo factory）

**Files:**
- Create: `crossborder_selector/web/demo.py`
- Test: `tests/test_web_demo.py`

**Interfaces:**
- `class DemoOrchestrator`：与 `Orchestrator` 相同的构造签名（含 `log, on_event, should_stop`），`run() -> RunResult`。每轮生成 `batch_size` 个候选（IP 从 `18.162.x.x`、`43.198.x.x`、`16.162.x.x` 轮换，prefix 对应 `/16`、`/15`、`/16`），第 1 轮固定一个 `reputation` 否决和一个 `reverse_unreachable`，其余随机 composite 55～97（用 `random.Random(run_id)` 保证可重复），按 `keep_top_k` 保留；每步之间 `sleep(step_delay)`（构造参数 `step_delay=0.4`，测试传 0）；尊重 `should_stop`；`target_score` 达到即停。返回的 `RunResult` 含完整 `CandidateScore`（`probe_results` 用一个 reverse `ProbeResult` 填三网 `IspProbe`，rtt 与 composite 一致），使 `write_reports` 生成真实格式报告。
- `demo_factory(cfg) -> dict`：返回假 client 集合，满足 `Api.env`（成功身份 `123456789012`、默认 VPC 2 子网、配额 64、运行中 3 台、无 winner）、`Api.options`（offerings 返回全部目录）、`Ec2Manager.protect/terminate/list_run_instances`（记录调用、返回空）。
- `demo_ensure_infra(ec2, iam, ssm, cfg) -> Infra("subnet-demo", "sg-demo", "crossborder-selector-ssm", "ami-demo")`。
- `install_demo(run_manager)`：把 `run_manager.orchestrator_factory` 设为 `DemoOrchestrator`，并把 `runs.ensure_infra`、`runs.build_backends`（返回 `[]`）、`runs.load_ip_ranges`（返回 `[]`）替换为 demo 版本（用模块属性赋值，函数返回一个 `restore()` 可撤销）。

- [ ] **Step 1: 写失败测试**

`tests/test_web_demo.py`：

```python
import json
from crossborder_selector.config import load_config
from crossborder_selector.report import write_reports
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import DemoOrchestrator, demo_factory, install_demo
from crossborder_selector.web.runs import RunManager


def test_demo_orchestrator_emits_events_and_reports(tmp_path):
    cfg = load_config(None, {"batch_size": 4, "max_rounds": 2, "keep_top_k": 2, "target_score": 99, "output_dir": str(tmp_path)})
    events = []
    orch = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-demo", log=lambda m: None,
                            on_event=events.append, should_stop=lambda: False, step_delay=0)
    rr = orch.run()
    assert [e["type"] for e in events][:3] == ["round_started", "candidates", "vetoed"]
    assert len(rr.rounds) == 2 and len(rr.winners) == 2 and rr.stop_reason == "max_rounds"
    vetoes = {v.veto_reason for v in rr.rounds[0].vetoed}
    assert {"reputation", "reverse_unreachable"} <= vetoes
    paths = write_reports(rr, cfg, out_dir=str(tmp_path))
    d = json.load(open(paths["json"]))
    assert d["winners"][0]["reverse_telecom"] is not None and d["candidates"]


def test_demo_is_deterministic_and_stoppable():
    cfg = load_config(None, {"batch_size": 3, "max_rounds": 3, "keep_top_k": 1})
    a = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=lambda: False, step_delay=0).run()
    b = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=lambda: False, step_delay=0).run()
    assert a.winners[0].composite == b.winners[0].composite
    calls = {"n": 0}
    def stop():
        calls["n"] += 1
        return calls["n"] > 1
    c = DemoOrchestrator(cfg, None, None, [], [], lambda ip: "", None, "xb-1", on_event=lambda e: None, should_stop=stop, step_delay=0).run()
    assert c.stop_reason == "cancelled" and len(c.rounds) == 1


def test_demo_factory_serves_api_env_and_options(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["ok"] and e["caller"]["account"] == "123456789012" and e["vcpu_quota"] == 64.0
    o = api.options("ap-east-1")
    assert len(o["instance_types"]) >= 9


def test_install_demo_runs_end_to_end(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    mgr = RunManager(api, str(tmp_path / "out"))
    restore = install_demo(mgr, step_delay=0)
    try:
        run_id = mgr.start({"region": "ap-east-1", "batch_size": 3, "max_rounds": 1})
        import time
        for _ in range(200):
            if mgr.get(run_id).state != "running":
                break
            time.sleep(0.02)
        rec = mgr.get(run_id)
        assert rec.state == "finished" and rec.winners and (tmp_path / "out" / run_id / "report.md").exists()
    finally:
        restore()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_web_demo.py -q`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 demo.py**

要点（实现者据此编写，保持与接口一致）：

- 候选生成：`ip = f"{base}.{rnd.randint(1,254)}.{rnd.randint(1,254)}"`，`base` 轮换 `["18.162", "43.198", "16.162"]`，prefix 分别 `"18.162.0.0/16"`、`"43.198.0.0/15"`、`"16.162.0.0/16"`；instance id `f"i-demo{round}{idx:02d}"`。
- 第 1 轮：索引 0 为 `reputation` 否决（`ReputationResult(ip, [SourceResult("dnsbl", True, "zen.spamhaus.org")], 50.0)`），索引 1 为 `reverse_unreachable`（`ProbeResult("reverse", [IspProbe(isp, 4, 0, None) ...])`，composite 0，qualified False）。
- 其余候选：`composite = round(rnd.uniform(55, 97), 1)`；rtt 反推 `rtt = 300 - (300-60) * composite/100`；`ProbeResult("reverse", [IspProbe(isp, 4, 4, rtt ± rnd.uniform(-8, 8))])`；`isp_scores` 与 `backend_scores={"reverse": composite}`。
- 事件顺序与 `Orchestrator` 一致：`round_started` → `candidates` → `vetoed` → `round_done`；`log` 用 `self.log(f"[demo round {n}] ...")`。
- 合并在位者：`rank(incumbents + scored)` 取前 `keep_top_k`；`terminated` 为其余 id。
- `should_stop()` 在每轮开始检查；`target_score` 达到即 `target_score_reached`。
- `demo_factory` 中假 client 的方法名与 `Api`、`Ec2Manager` 调用一致：`get_caller_identity`、`describe_vpcs`、`describe_subnets`、`describe_instances`（按 Filters 里是否有 `tag:crossborder-winner` 返回空或 3 台）、`get_service_quota`、`describe_instance_type_offerings`、`modify_instance_attribute`、`terminate_instances`、`create_tags`、`delete_tags`。
- `install_demo(mgr, step_delay=0.4)`：
  ```python
  import crossborder_selector.web.runs as runs_mod
  saved = (runs_mod.ensure_infra, runs_mod.build_backends, runs_mod.load_ip_ranges, mgr.orchestrator_factory)
  runs_mod.ensure_infra = demo_ensure_infra
  runs_mod.build_backends = lambda cfg, ssm: []
  runs_mod.load_ip_ranges = lambda cache_path=None: []
  mgr.orchestrator_factory = lambda *a, **kw: DemoOrchestrator(*a, step_delay=step_delay, **kw)
  def restore():
      runs_mod.ensure_infra, runs_mod.build_backends, runs_mod.load_ip_ranges, mgr.orchestrator_factory = saved
  return restore
  ```
  `runs.py` 中必须以 `ensure_infra(...)`、`build_backends(...)`、`load_ip_ranges(...)` 的模块级名字调用（Task 3 已如此），替换才生效。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_web_demo.py -q && pytest -q`
Expected: 4 passed；全量 123 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: demo mode with simulated orchestrator and fake AWS clients"
```

---

### Task 5: HTTP 服务、路由、SSE 与入口

**Files:**
- Create: `crossborder_selector/web/server.py`、`crossborder_selector/web/__main__.py`、`scripts/start_web.sh`
- Test: `tests/test_web_server.py`

**Interfaces:**
- `server.py`：`make_server(host, port, api: Api, runs: RunManager, static_dir=None, demo=False) -> ThreadingHTTPServer`；`Handler(BaseHTTPRequestHandler)` 通过 `server.ctx`（dict：api、runs、static_dir、demo）访问依赖。路由：
  - `GET /` → `static/index.html`（`Content-Type: text/html; charset=utf-8`）；`GET /api/meta` → `{"demo": bool, "version": "1.0"}`。
  - `GET /api/env?region=`、`GET /api/options?region=`、`POST /api/plan`、`POST /api/runs`、`GET /api/runs`、`GET /api/runs/{id}`、`GET /api/runs/{id}/events`、`POST /api/runs/{id}/cancel`、`POST /api/runs/{id}/select`、`GET /api/runs/{id}/report.(json|md|csv)`、`POST /api/cleanup`。
  - `ApiError` → 其 status；未知路径 404 `{"error":"未找到"}`；JSON 解析失败 400；其他异常 500 `{"error": "<type>: <msg>"}`。
  - SSE：响应头 `Content-Type: text/event-stream`、`Cache-Control: no-cache`；先回放 `record.events`，再从 `subscribe` 队列读，`queue.get(timeout=15)` 超时时发 `: keepalive\n\n`；遇到 `finished`/`failed` 事件后再发一条即结束；`BrokenPipeError`/`ConnectionResetError` 时 `unsubscribe` 并退出。run 不存在 → 404。
  - `POST /api/runs/{id}/select`：body `{instance_id, protect, terminate_others}`；`winner_ids = runs.winner_ids(id)`；`region = record.region`（无记录则从 `status.json` 读）；调用 `api.select(...)`；把选择结果写入 `out/<id>/selection.json`。
  - `POST /api/cleanup`：body `{run_id}`；region 同上；调用 `api.cleanup`。
  - `log_message` 静默（不刷屏）。
- `__main__.py`：argparse `--host 127.0.0.1 --port 8765 --output-dir ./out --demo --no-browser --config`；构建 `Api(factory=demo_factory if demo else default_factory, config_path=args.config)`、`RunManager(api, output_dir)`，demo 时 `install_demo(mgr)`；打印 `Web 向导：http://127.0.0.1:8765`（demo 加 `（演示模式，不接触 AWS）`）；非 `--no-browser` 时 `webbrowser.open`；`serve_forever()`，Ctrl-C 优雅退出。
- `scripts/start_web.sh [--demo] [--port N]`：进入仓库根，激活 `.venv`，`exec python -m crossborder_selector.web "$@"`。

- [ ] **Step 1: 写失败测试**

`tests/test_web_server.py`：

```python
import json
import threading
import urllib.request
import urllib.error

import pytest

from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import demo_factory, install_demo
from crossborder_selector.web.runs import RunManager
from crossborder_selector.web.server import make_server


@pytest.fixture
def srv(tmp_path):
    api = Api(factory=demo_factory, cwd=str(tmp_path))
    mgr = RunManager(api, str(tmp_path / "out"))
    restore = install_demo(mgr, step_delay=0)
    server = make_server("127.0.0.1", 0, api, mgr, demo=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, mgr
    server.shutdown()
    restore()


def _get(url, raw=False):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read()
        return (r.status, body) if raw else (r.status, json.loads(body))


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_index_and_meta(srv):
    base, _ = srv
    status, body = _get(base + "/", raw=True)
    assert status == 200 and b"<html" in body.lower() and "向导".encode() in body
    assert _get(base + "/api/meta")[1]["demo"] is True


def test_env_options_plan(srv):
    base, _ = srv
    assert _get(base + "/api/env?region=ap-east-1")[1]["ok"] is True
    assert _get(base + "/api/options?region=ap-east-1")[1]["instance_types"]
    st, p = _post(base + "/api/plan", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1})
    assert st == 200 and "3 x" in p["plan_summary"]
    st, e = _post(base + "/api/plan", {"batch_size": 0})
    assert st == 400 and "error" in e


def test_run_lifecycle_and_sse(srv):
    base, mgr = srv
    st, r = _post(base + "/api/runs", {"region": "ap-east-1", "batch_size": 3, "max_rounds": 1, "keep_top_k": 2})
    assert st == 200 and r["run_id"].startswith("xb-")
    rid = r["run_id"]
    with urllib.request.urlopen(base + f"/api/runs/{rid}/events", timeout=10) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        seen = []
        for line in resp:
            line = line.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                seen.append(ev["type"])
                if ev["type"] in ("finished", "failed"):
                    break
    assert seen[-1] == "finished" and "round_done" in seen
    st, detail = _get(base + f"/api/runs/{rid}")
    assert detail["state"] == "finished" and len(detail["winners"]) == 2
    assert _get(base + "/api/runs")[1][0]["run_id"] == rid
    st, body = _get(base + f"/api/runs/{rid}/report.csv", raw=True)
    assert st == 200 and body.startswith(b"run_id,")
    winners = [w["instance_id"] for w in detail["winners"]]
    st, sel = _post(base + f"/api/runs/{rid}/select", {"instance_id": winners[0], "protect": True, "terminate_others": True})
    assert st == 200 and sel["selected"] == winners[0] and sel["terminated"] == winners[1:]
    st, bad = _post(base + f"/api/runs/{rid}/select", {"instance_id": "i-nope", "protect": False, "terminate_others": False})
    assert st == 400
    st, cl = _post(base + "/api/cleanup", {"run_id": rid})
    assert st == 200 and cl["run_id"] == rid


def test_404_and_bad_json(srv):
    base, _ = srv
    try:
        urllib.request.urlopen(base + "/api/nope", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 404 and json.loads(e.read())["error"]
    req = urllib.request.Request(base + "/api/plan", data=b"{bad", headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_web_server.py -q`
Expected: FAIL，模块不存在（`index.html` 尚未创建也会导致 `test_index_and_meta` 失败，Task 6 完成后通过；本任务先放一个占位 `static/index.html`，内容仅一行 `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>跨境优选实例向导</title></head><body>向导加载中</body></html>`）

- [ ] **Step 3: 实现 server.py**

```python
"""标准库 HTTP 服务：路由、JSON 编解码、SSE。业务逻辑在 api.py / runs.py。"""
import json
import os
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from crossborder_selector.web.api import ApiError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9._-]+)(?:/(events|cancel|select|report\.(json|md|csv)))?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "crossborder-web/1.0"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "未找到"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(400, "请求体不是合法 JSON")

    @property
    def ctx(self):
        return self.server.ctx

    def _region_for(self, run_id):
        rec = self.ctx["runs"].get(run_id)
        if rec:
            return rec.region
        path = os.path.join(self.ctx["runs"].output_dir, run_id, "status.json")
        try:
            with open(path) as f:
                return json.load(f).get("region")
        except (OSError, ValueError):
            raise ApiError(404, "run 不存在")

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route("GET")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # 兜底：不让线程静默死掉
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    do_POST = do_GET

    def _route(self, method):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        api, runs = self.ctx["api"], self.ctx["runs"]
        p = u.path
        if method == "GET" and p == "/":
            return self._file(os.path.join(self.ctx["static_dir"], "index.html"), "text/html; charset=utf-8")
        if method == "GET" and p == "/api/meta":
            return self._json(200, {"demo": self.ctx["demo"], "version": "1.0"})
        if method == "GET" and p == "/api/env":
            return self._json(200, api.env(qs.get("region", ["ap-east-1"])[0]))
        if method == "GET" and p == "/api/options":
            return self._json(200, api.options(qs.get("region", ["ap-east-1"])[0]))
        if method == "POST" and p == "/api/plan":
            return self._json(200, api.plan(self._body()))
        if method == "POST" and p == "/api/runs":
            return self._json(200, {"run_id": runs.start(self._body())})
        if method == "GET" and p == "/api/runs":
            return self._json(200, runs.list())
        if method == "POST" and p == "/api/cleanup":
            body = self._body()
            rid = body.get("run_id") or ""
            return self._json(200, api.cleanup(rid, self._region_for(rid)))
        m = _RUN_RE.match(p)
        if not m:
            raise ApiError(404, "未找到")
        rid, sub, ext = m.group(1), m.group(2), m.group(3)
        if sub is None and method == "GET":
            rec = runs.get(rid)
            if rec:
                return self._json(200, rec.to_detail())
            path = os.path.join(runs.output_dir, rid, "status.json")
            if not os.path.exists(path):
                raise ApiError(404, "run 不存在")
            with open(path) as f:
                d = json.load(f)
            ev_path = os.path.join(runs.output_dir, rid, "events.jsonl")
            d["events"] = []
            if os.path.exists(ev_path):
                with open(ev_path) as f:
                    d["events"] = [json.loads(l) for l in f if l.strip()][-200:]
            if d.get("state") == "running":
                d["state"] = "unknown"
            return self._json(200, d)
        if sub == "events" and method == "GET":
            return self._sse(rid)
        if sub == "cancel" and method == "POST":
            if not runs.cancel(rid):
                raise ApiError(404, "run 不存在或已结束")
            return self._json(200, {"run_id": rid, "cancel_requested": True})
        if sub == "select" and method == "POST":
            body = self._body()
            result = api.select(rid, body.get("instance_id", ""), bool(body.get("protect")), bool(body.get("terminate_others")),
                                self._region_for(rid), runs.winner_ids(rid))
            with open(os.path.join(runs.output_dir, rid, "selection.json"), "w") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            return self._json(200, result)
        if sub and sub.startswith("report.") and method == "GET":
            ctype = {"json": "application/json; charset=utf-8", "md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8"}[ext]
            name = {"json": "report.json", "md": "report.md", "csv": "candidates.csv"}[ext]
            return self._file(os.path.join(runs.output_dir, rid, name), ctype)
        raise ApiError(404, "未找到")

    # ---------- SSE ----------
    def _sse(self, rid):
        runs = self.ctx["runs"]
        rec = runs.get(rid)
        if rec is None:
            raise ApiError(404, "run 不存在或服务已重启，请查看历史记录")
        q = runs.subscribe(rid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            replay = list(rec.events)
            for ev in replay:
                self._sse_write(ev)
            if replay and replay[-1]["type"] in ("finished", "failed"):
                return
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(ev)
                if ev["type"] in ("finished", "failed"):
                    return
        finally:
            runs.unsubscribe(rid, q)

    def _sse_write(self, ev):
        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()


def make_server(host, port, api, runs, static_dir=None, demo=False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.ctx = {"api": api, "runs": runs, "static_dir": static_dir or STATIC_DIR, "demo": demo}
    return server
```

`__main__.py`：

```python
"""python -m crossborder_selector.web：启动本地 Web 向导。"""
import argparse
import sys
import webbrowser

from crossborder_selector.cli import default_factory
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import demo_factory, install_demo
from crossborder_selector.web.runs import RunManager
from crossborder_selector.web.server import make_server


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="crossborder-web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--output-dir", default="./out")
    p.add_argument("--config")
    p.add_argument("--demo", action="store_true", help="演示模式：不接触 AWS，用模拟数据走完整流程")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    api = Api(factory=demo_factory if a.demo else default_factory, config_path=a.config)
    mgr = RunManager(api, a.output_dir)
    if a.demo:
        install_demo(mgr)
    server = make_server(a.host, a.port, api, mgr, demo=a.demo)
    url = f"http://{a.host}:{server.server_address[1]}"
    print(f"Web 向导：{url}" + ("（演示模式，不接触 AWS）" if a.demo else ""))
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。运行中的 run 线程随进程结束；如有遗留实例请用 cleanup --run-id 清理。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`scripts/start_web.sh`：

```bash
#!/usr/bin/env bash
# 启动本地 Web 向导。用法: scripts/start_web.sh [--demo] [--port 8765] [--no-browser]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
exec "$PY" -m crossborder_selector.web "$@"
```

- [ ] **Step 4: 运行确认通过**

Run: `chmod +x scripts/start_web.sh && pytest tests/test_web_server.py -q && pytest -q`
Expected: 4 passed；全量 127 passed

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat: local web server with routes, SSE and demo entrypoint"
```

---

### Task 6: 单页向导前端

**Files:**
- Create/Replace: `crossborder_selector/web/static/index.html`

**Interfaces:**
- Consumes: Task 5 的全部 API 与 SSE 事件格式；`GET /api/meta` 判断演示模式。

**要求**（实现者据此编写；视觉体系沿用 `ui/report-viewer.html`：IBM Plex Sans / IBM Plex Mono、浅深双主题 token、同一套 pill / tile / table 样式，可以直接复制其 `<style>` 并扩展）：

1. **骨架**：顶栏（产品名「跨境优选实例向导」、演示模式标记（`/api/meta.demo` 为真时显示深黄底「演示模式，不接触 AWS」）、Region 显示）；左侧步骤导航（六步，当前步高亮，已完成步可点回看）；主区域按步骤渲染；右侧固定「历史运行」列表（`GET /api/runs`，每 10 秒刷新，点击进入该 run 的结果页）。
2. **第 ① 步 环境检查**：进入即调 `/api/env?region=`；卡片显示：账号/ARN、默认 VPC（子网数）、vCPU 配额（null 显示"未知，不阻断"）、运行中实例数、已有 winner 列表；`problems` 非空以红色列出并禁用「下一步」；提供「重新检查」；Region 下拉在此步即可切换（选项来自 `/api/options.regions`）。
3. **第 ② 步 配置参数**：机型卡片网格（来自 `/api/options.instance_types`，显示 type、vCPU、内存、估算 $/h，单选，默认 `t3.nano`）；数字输入：每轮候选数（1～50）、最大轮次（1～10）、保留数（1～5，说明"保留多台可在结果页最终挑选"）、目标分（50～100）；backend 开关列表（`needs_key && !key_present` 时禁用并提示「在 config.yaml 填写 api_key 后可用」）；protect 复选（说明含义）；右侧「预估」面板随输入实时调 `/api/plan`（防抖 400ms）显示估算费用、时长、启用的 backend；「下一步」进入确认。
4. **第 ③ 步 确认计划**：等宽字体展示 `plan_summary`；醒目列出：将启动 `batch × rounds` 台候选、预计时长、估算费用、会创建/复用的 SG 与 instance profile；「开始运行」按钮点击弹确认（`confirm` 文案含台数与费用），确认后 `POST /api/runs`，记下 `run_id`，进入第 ④ 步。
5. **第 ④ 步 运行中**：`EventSource(/api/runs/{id}/events)`；进度条 = 已完成轮次 / max_rounds；已用时计时器；每轮一张卡片：启动 N 台 → 否决 M 台（列出 IP 与原因中文：信誉否决 / 三网全部丢包 / 无公网 IP）→ 保留（IP 与综合分）→ 终止数；下方日志区（自动滚动，最多保留 500 行）；「取消运行」按钮（confirm 后 `POST /cancel`，显示"已请求取消，当前轮结束后停止"）；收到 `finished` 自动进入第 ⑤ 步；收到 `failed` 显示错误与遗留实例 id，并提供「立即清理」按钮（`POST /api/cleanup`）。SSE 断开时用 `GET /api/runs/{id}` 轮询兜底（每 3 秒）。
6. **第 ⑤ 步 结果与选机**：`GET /api/runs/{id}` 与 `GET /api/runs/{id}/report.json`；顶部 tiles：停止原因（中文）、轮次、候选总数、winner 数；winner 卡片并排（IP、instance id、prefix、综合分、三网分条形、所在轮次），每张卡有「选定这台」；全部候选表（复用 report-viewer 的列与排序，点击表头排序）；prefix 统计表。「选定这台」弹确认：文案列出将被终止的其余 winner 实例 id（`terminate_others` 复选默认勾选）与是否开启 protect（复选默认跟随配置）；确认后 `POST /select`，进入第 ⑥ 步。若 winner 为 0：显示"本次没有合格候选"与常见原因（信誉全否决 / 三网不可达）及「回到配置重新运行」。
7. **第 ⑥ 步 完成**：选定实例 id、公网 IP、prefix、综合分、标签列表、是否已 protect；「接入建议」文字块：不要 stop；只让它承担代理/出口；解除 protect 的命令；报告下载三个按钮（`/report.json|md|csv`）；「查看历史运行」「再选一次」。
8. **状态恢复**：页面加载时 `GET /api/runs`，若有 `state==running` 的 run 则直接进入第 ④ 步跟随它；URL hash `#run=<id>` 可直达该 run 的结果页。
9. **文案**：全部中文，直接陈述；不用 emoji；金额一律带"估算"。所有按钮的 loading/disabled 状态明确；错误用顶部横幅显示 `error` 文本。
10. **实现约束**：单文件，无外部 JS 库；Google Fonts 链接同 report-viewer；用 `fetch` + `EventSource`；代码组织为 `state` 对象 + `render(step)` 函数 + 各步 `mount*` 函数；`textContent`/转义避免注入。

- [ ] **Step 1: 手工冒烟脚本**（无自动化测试；执行者按此验证并把每步截图/文字记录写入报告）

```bash
. .venv/bin/activate && python -m crossborder_selector.web --demo --no-browser --port 8790 &
sleep 1
curl -s http://127.0.0.1:8790/ | head -c 300
# 浏览器打开 http://127.0.0.1:8790 ，按六步走完：环境检查通过 → 选 t4g.nano、3 台、2 轮、保留 2 → 确认 → 观察进度与每轮卡片 → 结果页选定其中一台 → 完成页下载报告
kill %1
```

- [ ] **Step 2: 自动检查**

Run: `pytest tests/test_web_server.py -q`（`test_index_and_meta` 断言页面含"向导"）
Expected: 4 passed

- [ ] **Step 3: 提交**

```bash
git add -A && git commit -m "feat: six-step web wizard frontend"
```

---

### Task 7: 文档、skill 与入口更新

**Files:**
- Modify: `README.md`、`MANUAL.md`、`TESTING.md`、`.claude/skills/crossborder-select/SKILL.md`

- [ ] **Step 1: README**
  - 「快速开始」增加 `scripts/start_web.sh` 与 `scripts/start_web.sh --demo` 两行及说明。
  - 新增小节「Web 向导」：六步流程一句话各一条；演示模式说明；只绑定 127.0.0.1、不做认证。
  - 「项目结构」加入 `crossborder_selector/web/` 与 `scripts/start_web.sh`。
  - 「文档」表加 TESTING 的 Web 冒烟章节引用。
- [ ] **Step 2: MANUAL**：新增一节「用 Web 向导运行」，与 CLI 并列：启动、六步说明、取消与失败清理、历史运行、演示模式；故障排查表加「端口被占用」（换 `--port`）、「页面打不开」（确认只在本机访问）。
- [ ] **Step 3: TESTING**：新增「Web 向导」章节：`pytest tests/test_web_*.py -q` 预期数量；演示模式手工冒烟步骤（Task 6 Step 1）；真实模式冒烟（`scripts/start_web.sh`，2 台 1 轮）；SSE 用 `curl -N http://127.0.0.1:8765/api/runs/<id>/events` 验证。
- [ ] **Step 4: SKILL.md**：命令速查加 `scripts/start_web.sh [--demo]`；执行流程加一条"用户要图形界面或要给客户演示时，启动 Web 向导并给出地址；无凭证环境用 `--demo`"。
- [ ] **Step 5: 全量测试并提交**

```bash
pytest -q
git add -A && git commit -m "docs: web wizard usage, demo mode, testing and skill update"
```

---

## 自查记录

**Spec 覆盖：** §2 六步 → Task 6；§3 API → Task 2/5；§4 事件与钩子 → Task 1/3；§5 模块 → 文件结构表；§6 前端 → Task 6；§7 错误处理 → Task 3（failed/leftover、unknown）、Task 5（404/400/500）、Task 6（横幅、清理按钮、轮询兜底）；§8 测试 → Task 1–5 测试文件，前端手工冒烟 → Task 6/7；§9 文档与入口 → Task 5（start_web.sh）、Task 7；§10 演示模式 → Task 4/5。

**类型一致性：** `Orchestrator(..., log, on_event, should_stop)` 在 Task 1/3/4 一致；`RunManager.start(overrides) -> run_id`、`get/list/winner_ids/subscribe/unsubscribe/cancel` 在 Task 3/5 一致；`Api.select(run_id, instance_id, protect, terminate_others, region, winner_ids)` 与 Task 5 调用一致；事件类型集合与 Global Constraints 一致。

**已知取舍：** 前端不做自动化测试，仅由 `test_index_and_meta` 保证页面可服务；`Api.env` 需要 `sts`、`service-quotas` client，通过 `_client_factory_with_quotas` 在非注入场景懒创建。
