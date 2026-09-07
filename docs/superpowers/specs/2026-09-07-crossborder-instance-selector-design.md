# AWS 跨境优选实例选择器 — 设计文档

日期：2026-09-07
状态：已与需求方确认，进入实施计划阶段

## 1. 背景与目标

AWS 没有"跨境优选 IP"这类服务。EC2 自动分配的公网 IPv4 无法转换为 EIP；EIP 的分配器会反复返回同一地址，且受每 Region 5 个的配额限制。因此"选一个好 IP"在 AWS 上最可靠的落地方式是：**批量启动临时 EC2，测其自动分配的公网 IPv4 从中国大陆访问的质量，保留胜出的实例，终止其余**。只要胜出实例持续运行（reboot 不换 IP，stop/start 会换），这个 IP 就一直可用。

本工具（仓库 `aws-crossborder-instance-selector`，Python 包 `crossborder_selector`）实现这一流程，并保留姊妹项目 `clean-ip-selection` 中的 IP 信誉预筛能力。

目标：

- 一条命令在指定 Region 多轮启动候选 EC2，从大陆视角拨测，自动保留全局 Top-K 实例。
- 拨测数据源可插拔，默认零外部依赖，不依赖阿里云、腾讯云等国内云厂商服务。
- 任一黑名单命中的 IP 直接淘汰。
- 输出 JSON、Markdown、CSV 三种报告，并记录候选 IP 所属 AWS prefix 的历史统计。
- 所有临时资源按 run-id 打标签，可一键清理；胜出实例不受清理影响。

非目标：

- 不做 EIP 筛选（姊妹项目已覆盖）。
- 不做长期监控与自动换机。
- 不做基于 prefix 历史的自动跳过（仅记录，留作后续扩展）。
- 不接入任何需要付费或国内云账号的拨测 API。

## 2. 整体流程

```
select
 ├─ 准备基础设施（幂等、跨 run 复用、免费）
 │    默认 VPC 与子网 → 无入站 SG → IAM role + instance profile（AmazonSSMManagedInstanceCore）
 ├─ for round in 1..max_rounds
 │    ① 启动 batch_size 台候选 EC2：AL2023 最新 AMI、机型可配、自动分配公网 IPv4、
 │       标签 crossborder-run-id=<run-id>、crossborder-round=<n>
 │    ② 等待 running + SSM online；读取公网 IP；用 ip-ranges.json 归类 prefix
 │    ③ 信誉预筛：DNSBL / badlist / AbuseIPDB，任一命中 → 立即终止
 │    ④ 并行拨测（ProbeBackend 列表）
 │         reverse    候选机经 SSM 对三网目标 ping + tcping     默认开启
 │         globalping 从 HK/TW 公共探针打候选 IP                  默认开启
 │         ripeatlas  从大陆在线探针 ping 候选 IP                 配置 API key 后开启
 │         itdog      三网家宽视角 ping/tcping 候选 IP            显式开启才用
 │    ⑤ 打分 → 与在位 winner 合并 → 全局 Top-K 保留，其余终止
 │    ⑥ best_score >= target_score 或 round == max_rounds → 停止
 ├─ winner 处理：删除 crossborder-run-id 标签；打 crossborder-winner=true、
 │    crossborder-score、crossborder-round、crossborder-selected-at 标签
 └─ 输出 out/<run-id>/report.json、report.md、candidates.csv；
      追加 history/prefix_stats.json
```

### 成本量级

香港 t3.nano 加公网 IPv4 每台每小时约 0.012 美元。每轮候选机存活 5～8 分钟。20 台 × 3 轮总成本低于 0.5 美元。胜出实例长期运行按机型正常计费，公网 IPv4 每小时 0.005 美元。

## 3. 仓库结构

```
aws-crossborder-instance-selector/
  crossborder_selector/
    __init__.py
    cli.py               select / cleanup / report 子命令，run-id 生成与打印
    config.py            YAML → Config dataclass，校验 weights、backends、targets
    models.py            Candidate、ProbeResult、IspScore、CandidateScore、RoundResult
    orchestrator.py      锦标赛循环；只编排，不直接调云 API
    aws/
      ec2.py             launch / describe / terminate / tag / untag，全部按 run-id 过滤
      infra.py           默认 VPC、SG、IAM role、instance profile 的幂等创建
      ssm.py             send_command + 轮询 get_command_invocation
      ipranges.py        下载并缓存 ip-ranges.json，IP → prefix 映射
    reputation/          从 clean-ip-selection 原样移植：base / dnsbl / badlist / abuseipdb
    probes/
      base.py            ProbeBackend 抽象：name、enabled(config)、probe(candidates) → {ip: ProbeResult}
      reverse.py         生成三网探测 shell 脚本，经 SSM 执行，解析 ping/tcping 输出
      globalping.py      api.globalping.io v1 measurements，HK/TW 探针
      ripeatlas.py       atlas.ripe.net v2 一次性 ping 测量，country=CN
      itdog.py           封装非官方 WebSocket 协议；任何失败只降级不阻塞
    scoring.py           子分归一化、ISP 加权、backend 加权、硬门限、排序
    report.py            JSON / Markdown / CSV 输出；prefix 历史追加
  scripts/
    find_best_instance.sh   一条命令入口：生成临时 config、调用 cli、退出时清理临时文件
  tests/                 pytest；EC2/IAM 用 moto；SSM 与四个 backend 注入假 transport
  config.example.yaml
  requirements.txt
  pytest.ini
  README.md
  MANUAL.md
  docs/superpowers/specs/   本文档
```

## 4. 配置模型

```yaml
region: ap-east-1
instance_type: t3.nano
image_id: ""              # 空则通过 SSM 参数解析最新 AL2023 x86_64 AMI
subnet_id: ""             # 空则用默认 VPC 的第一个子网
security_group_id: ""     # 空则自动创建/复用 crossborder-selector-sg
instance_profile_name: "" # 空则自动创建/复用 crossborder-selector-ssm

batch_size: 10
max_rounds: 3
keep_top_k: 1
target_score: 90          # 达到即提前停止
min_backends: 1           # 有效 backend 少于此值 → 不合格

ssm_online_timeout_s: 180

backends:
  reverse:
    enabled: true
    ping_count: 10
    tcping_count: 5
    tcping_port: 443
    targets:                                   # 条目可为 host 或 host:port
      telecom: ["114.114.114.114:53", "www.189.cn"]
      unicom:  ["123.123.123.123:53", "www.10010.com"]
      mobile:  ["221.130.33.52:53", "www.10086.cn"]
  globalping:
    enabled: true
    locations: ["HK", "TW"]
    limit_per_location: 3
  ripeatlas:
    enabled: false
    api_key: ""
    probe_count: 10
  itdog:
    enabled: false
    node_ids: []          # 空则用内置的三网默认节点

weights:
  backends: {reverse: 0.5, globalping: 0.2, ripeatlas: 0.15, itdog: 0.15}
  isps:     {telecom: 0.34, unicom: 0.33, mobile: 0.33}
  lat_good_ms: 60
  lat_bad_ms: 300

reputation:
  dnsbl_zones: ["zen.spamhaus.org", "b.barracudacentral.org"]
  badlist_url: "https://raw.githubusercontent.com/mitchellkrogza/nginx-ultimate-bad-bot-blocker/master/_generator_lists/bad-ip-addresses.list"
  abuseipdb_api_key: ""

output_dir: ./out
history_file: ./history/prefix_stats.json
```

校验规则：

- `weights.backends` 的键必须是已知 backend 名的子集；`weights.isps` 必须恰好为三网。
- `lat_good_ms < lat_bad_ms`。
- `keep_top_k >= 1`，`batch_size >= 1`，`max_rounds >= 1`。
- `reverse.targets` 三个 ISP 各至少一个目标。

CLI 覆盖项：`--region`、`--batch-size`、`--max-rounds`、`--keep-top-k`、`--target-score`、`--instance-type`、`--enable-backend name`、`--disable-backend name`、`--protect`、`--dry-run`。

## 5. 数据模型

```python
@dataclass
class Candidate:
    instance_id: str
    public_ip: str
    prefix: str            # 来自 ip-ranges.json，如 "18.162.0.0/16"；未匹配为 ""
    round: int
    launched_at: datetime

@dataclass
class IspProbe:            # 一个 backend 对一个 ISP（或 HK/TW 地区）的原始结果
    isp: str               # telecom / unicom / mobile / HK / TW
    sent: int
    received: int
    median_rtt_ms: float | None
    @property loss(self) -> float

@dataclass
class ProbeResult:
    backend: str
    probes: list[IspProbe]
    error: str = ""        # 非空表示该 backend 对该候选失败

@dataclass
class CandidateScore:
    candidate: Candidate
    reputation: ReputationResult      # 移植自 clean-ip-selection
    probe_results: list[ProbeResult]
    isp_scores: dict[str, float]      # 汇总后每 ISP 0~100
    backend_scores: dict[str, float]  # 每 backend 0~100
    composite: float                  # 0~100
    qualified: bool
    veto_reason: str = ""

@dataclass
class RoundResult:
    round: int
    launched: list[Candidate]
    vetoed: list[CandidateScore]
    scored: list[CandidateScore]
    kept: list[CandidateScore]
    terminated: list[str]
    backend_errors: dict[str, str]
```

## 6. 打分模型

### 6.1 硬否决（veto）

按顺序判断，任一成立即 `qualified=False` 并记录 `veto_reason`：

1. `reputation.any_listed` 为真（预筛阶段即终止，不进拨测）。
2. reverse backend 启用且三网全部 `received == 0`。
3. 返回有效数据（无 error 且至少一个 IspProbe）的 backend 数量 < `min_backends`。

### 6.2 子分

对每个 IspProbe：

```
lat_factor = 1                              若 rtt <= lat_good_ms
           = 0                              若 rtt >= lat_bad_ms 或 rtt 为 None
           = (lat_bad - rtt) / (lat_bad - lat_good)   否则
isp_probe_score = 100 × (1 − loss) × lat_factor
```

同一 backend 对同一 ISP 有多个目标时取算术平均。

### 6.3 合成

- backend 分：该 backend 覆盖的 ISP 分按 `weights.isps` 加权；globalping 的 HK/TW 不属于三网，等权平均。
- composite：按 `weights.backends` 加权，只对**返回了有效数据**的 backend 归一化权重，缺失 backend 不计为零分。
- 排序键：`(qualified, composite, backend_scores.get("reverse", 0))` 降序。

### 6.4 prefix

prefix 只记录，不参与打分。报告与历史文件按 prefix 汇总均值、样本数、最高分。

## 7. 反向探测（reverse backend）

在候选机上经 SSM `AWS-RunShellScript` 执行一段生成的 bash：

- 目标条目可为 `host` 或 `host:port`；ping 始终只用 host 部分，tcping 用条目端口，未给端口则回退到 `tcping_port`。
- 对每个目标：`ping -c <ping_count> -W 2`，解析 `received` 与 rtt 中位数（用 `-D` 时间戳或直接解析 summary 行的 avg，取 avg 作为近似中位数）。
- tcping：`for i in 1..tcping_count; do timeout 3 bash -c "echo > /dev/tcp/<host>/<port>"` 记录成功次数和耗时（`date +%s%N` 差值）。
- 输出一行 JSON，SSM 结果里以 `CROSSBORDER_JSON:` 前缀标记，便于解析。
- SSM 命令超时 120 秒；超时或非零退出 → `ProbeResult.error`。

文档中明确：这是"候选机 → 大陆"的出境路径近似，探测目标是运营商机房而非家宽用户；ICMP 可能被限速，tcping 作为补充；建议对 finalists 用 itdog 插件二次确认。

## 8. 外部探测 backend

### 8.1 globalping

- `POST https://api.globalping.io/v1/measurements`，type=ping，target=候选 IP，locations 为每个配置地区 `{"country": "HK", "limit": n}`。
- 轮询 `GET /v1/measurements/{id}` 直到 status=finished，超时 60 秒。
- 每个探针结果映射为一个 IspProbe（isp = 地区码）。
- 无需 API key；遵守速率限制，候选间串行提交、并行轮询。

### 8.2 ripeatlas

- `POST https://atlas.ripe.net/api/v2/measurements/`，one-off ping，probes `{"type":"country","value":"CN","requested":probe_count}`，需 `api_key`。
- 轮询 results 端点最多 120 秒。
- 每个探针结果按 ASN 映射到 ISP：4134/4812/23724 → telecom，4837/4808/17816 → unicom，9808/56040~56048/24444/24445 → mobile，其余标记 `other` 并等权计入。
- 未配置 key 时 `enabled()` 返回 False。

### 8.3 itdog

- 封装非官方 WebSocket 协议（参考社区实现 itdog-skill）。默认节点：三网各一个北京/上海/广州节点。
- 协议变化、连接失败、超时全部只写 `ProbeResult.error`，不抛出到 orchestrator。
- README 标注：非官方接口，可能随时失效，默认关闭。

## 9. 基础设施与安全边界

- **SG**：名称 `crossborder-selector-sg`，无任何入站规则，出站全开。按名称查找，不存在则创建，打 `crossborder-managed=true` 标签。
- **IAM**：role `crossborder-selector-ssm-role` 仅附加 `AmazonSSMManagedInstanceCore`；instance profile 同名。幂等创建，创建后等待 IAM 传播（最多 30 秒重试 RunInstances 的 InvalidParameterValue）。
- **AMI**：SSM 公共参数 `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`（arm 机型用 arm64 参数）。AL2023 自带 SSM agent。
- **默认 VPC 缺失**：报错并提示手工指定 `subnet_id` 与 `security_group_id`，不自动创建 VPC。
- **标签**：所有候选机 `crossborder-run-id`、`crossborder-round`。`cleanup --run-id` 只终止带该 run-id 的实例；默认不删 SG 和 instance profile（winner 依赖），`cleanup --include-infra` 才删且要求当前无 `crossborder-winner=true` 实例。
- **winner**：选出后立即 `delete_tags` 移除 run-id，再 `create_tags` 打 winner 标签。`--protect` 开启时对 winner 调用 `modify_instance_attribute` 设置 `DisableApiStop=true` 与 `DisableApiTermination=true`；默认关闭。
- **凭证**：工具只需 EC2、IAM（创建 role/profile）、SSM 权限；不向候选机注入任何 AWS 凭证。

## 10. 报告

输出目录 `out/<run-id>/`：

- `report.json`：run 元数据（region、config 摘要、开始/结束时间、轮数）、每轮 RoundResult、最终 winners、backend_errors、prefix 汇总。
- `report.md`：人读摘要：winners 表（instance id、IP、prefix、composite、三网分）、每轮淘汰概览、备注（不要 stop 该实例；如何删除 winner 标签等）。
- `candidates.csv`：一行一个候选，列：run_id、round、instance_id、public_ip、prefix、reputation_score、veto_reason、composite、qualified、reverse_telecom、reverse_unicom、reverse_mobile、globalping_HK、globalping_TW、ripeatlas_telecom、ripeatlas_unicom、ripeatlas_mobile、itdog_telecom、itdog_unicom、itdog_mobile、kept、terminated_at。缺失填空。

`history/prefix_stats.json`：`{prefix: {samples, mean_composite, best_composite, last_seen}}`，每次 run 结束合并更新。`report --run-id` 子命令可从 report.json 重新生成 md/csv。

## 11. 错误处理

| 情况 | 处理 |
|---|---|
| 候选机 SSM 在超时内未 online | reverse 记 error；其余 backend 正常；若最终不合格则终止 |
| 某 backend 抛异常 | 写入 `backend_errors[backend]`，本轮继续 |
| RunInstances 配额/容量不足 | 本轮 batch 缩小为实际成功数量，报告标注；不重试整轮 |
| ip-ranges.json 下载失败 | prefix 置空，继续 |
| 信誉源单个失败 | 沿用移植逻辑：不扣分、不判命中 |
| 任何未捕获异常 | `finally` 终止本 run 所有非 winner 实例；打印 run-id 供 `cleanup` |
| `--dry-run` | 打印将启动的实例数、AMI、SG/profile 名称、启用的 backend、目标列表；不创建资源、不需凭证 |

## 12. 测试策略

- `tests/test_scoring.py`：veto 三种情况、lat_factor 边界、缺失 backend 的权重归一化、排序键。
- `tests/test_config.py`：默认值填充、校验规则、CLI 覆盖。
- `tests/test_ec2.py`、`test_infra.py`：moto 模拟 launch/tag/untag/terminate、SG 与 profile 幂等、cleanup 只按标签删。
- `tests/test_ssm.py`、`test_probe_reverse.py`：注入假 SSM client，验证脚本生成与 `CROSSBORDER_JSON:` 解析，含超时路径。
- `tests/test_probe_globalping.py` / `ripeatlas` / `itdog`：注入假 HTTP/WebSocket transport，验证请求体、轮询、错误降级。
- `tests/test_orchestrator.py`：假 provider 与假 backend，验证多轮保留/终止、target_score 提前停止、winner 标签切换、finally 清理。
- `tests/test_report.py`：三种格式字段完整性、prefix 历史合并。
- `tests/test_wrapper_script.py`：dry-run 路径与临时 config 清理。
- 不做真实网络与真实云测试；真实验证在 MANUAL 中以 `batch_size=2, max_rounds=1` 的冒烟步骤描述。

## 13. 分阶段实施

1. 骨架：仓库、包结构、config、models、reputation 移植、dry-run。
2. AWS 层：infra、ec2、ssm、ipranges，moto 测试。
3. reverse backend + scoring + 单轮 orchestrator。
4. 多轮锦标赛、winner 标签、cleanup、report 三格式。
5. globalping backend。
6. ripeatlas、itdog backend（可选，默认关闭）。
7. 包装脚本、README、MANUAL、真实冒烟。
