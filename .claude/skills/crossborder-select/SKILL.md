---
name: crossborder-select
description: 驱动 aws-crossborder-instance-selector 仓库：批量启动候选 EC2、从香港/台湾及大陆视角测量公网 IPv4、按信誉和丢包延迟保留最优实例，并能对已有 IP 做单点探测与黑名单检查。Use when 用户提到跨境优选 IP、找一台到中国大陆网络质量好的 EC2、运行 crossborder selector 或 find_best_instance.sh、启动 Web 向导、查看或清理某次 run、解读 report.json 里的 veto_reason / stop_reason / reputation_status，或要检查某个已有 IP 的 Globalping 连通性和 DNSBL、Spamhaus、Barracuda、AbuseIPDB 信誉。
compatibility: 需要 Python 3.11+ 与项目 .venv；探测和信誉检查需公网访问（Globalping、GitHub、DNSBL）；真实筛选与反向探测需要 AWS 凭证，走 boto3 默认链，用 AWS_PROFILE 环境变量指定（select/cleanup 没有 --profile 参数，只有 probe_ip.py 有）。
metadata:
  project: aws-crossborder-instance-selector
  version: "2.0"
---

# crossborder-select

## 概述

工具在指定 AWS 区域批量创建临时 EC2，查询公网 IPv4 的信誉名单，从外部探针和实例自身两个方向测量网络，按 `100 × (1 − 丢包率) × 延迟因子` 打分，保留前 K 台并终止其余。结果是一次测量快照，不代表长期质量或业务可用性。

所有相对路径从仓库根目录（含 `crossborder_selector/` 和 `scripts/find_best_instance.sh`）执行，Python 用项目 `.venv`。首次使用先 `cp config.example.yaml config.yaml`；仓库默认只有 example 文件，没有 `config.yaml` 时脚本静默使用内置默认值，填在 example 里的配置不会生效。

## 何时使用

- 用户要在某区域"找一台跨境网络好的 EC2"或"筛一批干净 IP"。
- 用户要看、续做或清理某次 run（`xb-` 开头的运行 ID）。
- 用户拿着 `report.json` 问某个候选为什么被淘汰。
- 用户要检查一个已有 IP 的连通性或是否在黑名单，用本 skill 的 `scripts/probe_ip.py`。

**不适用**：`select` 流程不能把已有实例当候选，只能新建；工具不部署业务、不开服务端口、不做长期监控；非 AWS 的机器只能用 probe_ip 做外部探测和信誉检查，反向探测依赖 SSM。

## 命令速查

| 目的 | 命令 |
|---|---|
| 查看计划（不碰 AWS） | `scripts/find_best_instance.sh <region> <batch> <rounds> <keep> --dry-run` |
| 最小真实运行 | `scripts/find_best_instance.sh <region> 2 1 1` |
| 多轮并保护保留实例 | `scripts/find_best_instance.sh <region> 20 3 1 --protect` |
| 只用反向探测（不开 ICMP 入站） | 追加 `--disable-backend globalping` |
| 指定子网 / 镜像 / 系统盘 | 追加 `--subnet-id subnet-xxx`、`--image-id ami-xxx`、`--root-volume-size-gib 30`；或写入 `config.yaml` 顶层键 `subnet_id`、`image_id` |
| 启用可选探测源 | 追加 `--enable-backend itdog`；ripeatlas 还需 `config.yaml` 填 `api_key` |
| 本地 Web 向导 | `scripts/start_web.sh`（默认 http://127.0.0.1:8765）；演示用 `--demo` |
| 清理未保留候选 | `.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id>` |
| 重生成报告 | `.venv/bin/python -m crossborder_selector.cli report --run-id <run-id> --output-dir ./out` |
| 检查已有 IP | `.venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py <ip>` |
| 单元测试 | `.venv/bin/python -m pytest -o addopts= -q` |

位置参数依次是区域、每轮候选数（1–50）、最大轮次（1–10）、保留数。CLI 参数覆盖 `config.yaml` 同名项。

## 真实筛选工作流

把清单复制到回复里逐项勾选：

```
- [ ] 1 与用户确认区域、每轮数量、轮次、保留数、是否 --protect；真实运行产生费用，保留实例持续计费
- [ ] 2 确认 config.yaml 存在（没有就 cp config.example.yaml config.yaml）；无默认 VPC 时填顶层键 `subnet_id: subnet-xxx`（须有出网路径且自动分配公网 IPv4），或命令行追加 --subnet-id
- [ ] 3 跑 --dry-run，核对区域、数量、探测源、目标和安全组说明
- [ ] 4 确认凭证：export AWS_PROFILE=<profile>（需要时）后 aws sts get-caller-identity；区域为 opt-in 时确认账户已启用
- [ ] 5 真实运行；第一行输出 run-id，立即记下并告知用户
- [ ] 6 读 out/<run-id>/report.md：winners、stop_reason、每轮淘汰数、backend_errors
- [ ] 7 提醒：保留实例不要 stop/start（换公网 IP），reboot 通常保留；实例内关机被设为 stop
- [ ] 8 按 run-id 清理并用标签核查残留。正常结束时工具已终止非保留候选，cleanup 是幂等兜底；中断（退出码 130）或异常后必做
```

核查残留：

```bash
aws ec2 describe-instances --region <region> \
  --filters Name=tag:crossborder-run-id,Values=<run-id> \
  --query 'Reservations[].Instances[].{id:InstanceId,state:State.Name,winner:Tags[?Key==`crossborder-winner`]|[0].Value}' --output table
```

带 `crossborder-winner=true` 的是保留实例，cleanup 会跳过它们。

**安全组**：启用任一外部探测源时使用 `crossborder-selector-ping-sg`，向 `0.0.0.0/0` 开放 IPv4 ICMP Echo Request，不开 TCP/UDP；只有反向探测时使用无入站规则的组。安全组和 IAM 实例配置跨运行复用，普通 cleanup 不删。

## 检查已有 IP

`scripts/probe_ip.py` 复用项目的信誉与探测模块，不创建 AWS 资源：

```bash
# 信誉（DNSBL + GitHub 名单，配了 abuseipdb_api_key 或 ABUSEIPDB_API_KEY 时含 AbuseIPDB）+ 香港/台湾探针 ping
.venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200
# 指定探针地区、数量、包数
.venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200 --locations HK,TW,CN --limit 3 --packets 8
# 追加反向探测：该 EC2 经 SSM 向大陆三网目标 ping/tcping，实例须受 SSM 管理；--region 填实例实际所在区域
.venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200 \
  --instance-id i-xxx --region ap-east-2 --profile personal
```

退出码 0 表示未命中名单且探测合格，1 表示命中或被否决，2 表示错误。`--json` 输出完整结构。

- Globalping 显示 100% 丢包时先查该 IP 的安全组是否放行 ICMP Echo Request，不要直接判定网络差。
- 项目默认的"大陆视角"是反向探测（EC2 发往大陆目标）。Globalping 的 `CN` 探针数量少且不是住宅宽带，只代表数据中心出口可达性。
- ICMP 可达不等于 UDP/TCP 业务端口不受限，业务可用性仍需客户端实测。

## 读报告

`out/<run-id>/report.json` 是完整数据，`report.md` 供阅读，`candidates.csv` 不含探测明细。Web 的后续选定与终止记录在 `selection.json`。报告是快照，不证明实例当前状态。

**stop_reason**：`target_score_reached` 达标提前停止；`max_rounds` 轮次用尽；`no_qualified` 无合格候选，一台不留；`launch_failed` 后续轮启动失败但保留在位者；`cancelled` 用户取消。

**veto_reason**（按判定顺序，命中一个即停）：

| 值 | 含义 | 先查什么，怎么修 |
|---|---|---|
| `no_public_ip` | 未分配公网 IP | 子网是否自动分配公网 IPv4；换子网或开启自动分配后重跑 |
| `reputation` | 任一信誉源命中 | `reputation_results[]` 中 `listed=true` 的 source 和 detail；属正常淘汰，多跑几轮换 IP |
| `reputation_unavailable` | GitHub 名单检查失败且 `require_badlist: true`，未进入探测 | `badlist_url` 是否返回原始文本；修 URL 或出网后重跑 |
| `reverse_unavailable` | 反向探测无结果或解析失败 | SSM 是否 Online、实例角色、出网路径、`probe_results` 的 error |
| `reverse_incomplete` | 反向探测返回了，但样本未同时覆盖 telecom/unicom/mobile | 对比报告内 `config.backends.reverse.targets` 与当前配置，三个键都要有目标；补全后重跑，旧候选已终止不能补测 |
| `reverse_unreachable` | 反向探测全部目标 0 回包 | 目标是否可达、ICMP 是否被限速；换 `targets`，不要直接判定整个运营商不可达 |
| `min_backends` | 有效探测源数量不足 | 各探测源 error、Globalping 429；降低 `min_backends` 或修复探测源 |
| `unreachable` | 所有有效探测均无响应 | 安全组 ICMP、NACL、路由 |

被淘汰的候选实例已终止，修完配置只能重新 `select`，不能对旧候选补测。

**reputation_status**：`listed` 命中；`clear` 全部源完成且未命中；`unknown` 未命中但至少一个源没完成，此时 `reputation_score` 为 null。定位来源：在 `reputation_results[]` 里找 `status` 既不是 clear 也不是 listed 的条目，读它的 `error`（DNS 超时、SERVFAIL、`zone=127.255.255.254` 之类）或以 `error:` 开头的 `detail`；AbuseIPDB 配额耗尽会以 HTTP 429 出现在 `error`。一个候选若 veto 不是 `reputation_unavailable` 却带 `unknown`，则来源只可能是 DNSBL 或 AbuseIPDB，不是 GitHub 名单。手工复现 DNSBL：

```bash
dig +short 200.150.213.43.zen.spamhaus.org A     # IP 反写后拼 zone
```

NXDOMAIN 未命中；`127.0.0.x` 命中；`127.255.255.254/255` 是解析器被 Spamhaus 拒绝或限速，不是命中，应改用非公共递归解析器或换 zone。

历史规律：Barracuda 是目前唯一实际产生命中的源，且集中在特定新收购网段（如 ap-east-1 的 95.40.0.0/15）。命中只反映邮件滥用记录，不代表业务平台会拒绝该 IP。

## Python API 要点

详见 `docs/python-api.md` 与 `examples/api_client.py`，只记非显而易见的规则：

- `examples/api_client.py` 无参数只看计划；`start` 必须加 `--execute` 才创建。Python 类的 `client.start(config)` 直接创建。
- 创建接口没有跨请求幂等键。请求超时后先查 `runs`，不要自动重试创建。`wait` 超时或客户端中断不停止服务端运行。
- AMI 绑定区域和架构，不能跨区域复用；混合架构用 `image_preset` 按台解析，不要把一种架构的 AMI 复制给全部机型。
- 多机型用 `instance_groups` 或重复 `--group t3.nano=2 --group t4g.nano=3`，与单机型的 `--instance-type`/`--count` 不同时使用；保留数按全部机型合计。
- 镜像查询失败时保留手动输入途径，不虚构 AMI，不静默替换用户选的系统。

## 常见错误

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 把 dry-run 通过当作权限、配额、网络已验证 | 真实运行时 RunInstances 失败 | dry-run 只读配置生成文本，凭证和配额另行检查 |
| 把 subnet_id、api_key 填进 config.example.yaml | 脚本找不到 config.yaml，静默用默认值 | 先 `cp config.example.yaml config.yaml` 再填 |
| 给 find_best_instance.sh 传 `--profile` | 参数不存在，直接报错 | select/cleanup 用 `AWS_PROFILE` 环境变量 |
| 对保留实例 stop/start | 自动分配的公网 IPv4 更换 | 需要重启用 reboot；要固定地址需自行申请 EIP |
| 删掉 `crossborder-winner` 标签后再 cleanup | 保留实例被当作候选终止 | 不为整理标签而删该标记 |
| 创建请求超时后重试 `start --execute` | 重复创建两批实例 | 先 `runs` 查询是否已创建 |
| 把 Globalping 100% 丢包当网络差 | 误淘汰 | 先查目标安全组是否放行 ICMP |
| 把 reputation `unknown` 当 clear | 误保留信誉未知的 IP | unknown 是"未完成检查"，默认不保留 |
| `cleanup --include-infra --include-iam` 时还有 run 在跑 | 共享角色被删导致新实例 SSM 失联 | 清理共享资源期间停止发起新运行 |
| 用 `--allow-remote` 暴露 Web | 无认证，可操作实例 | 默认只监听 127.0.0.1，远程访问需网络层限制 |

## 安全边界

- 不手工 stop/terminate 带 `crossborder-winner=true` 的实例，除非用户明确点名该实例和操作。
- 不向候选机注入操作者 AWS 凭证；实例经 IAM 角色取 SSM 临时凭证。
- 不在对话、报告、日志中输出 `api_key`、`api_token`、`abuseipdb_api_key`。
- 不承诺固定的运行时长、费用或 Globalping 额度；以当次输出和提供方当前规则为准。

## 参考文件（仓库内）

- `README.md` 原理、评分、配置项、权限清单；`MANUAL.md` 操作与排查表；`TESTING.md` 验证方法。
- `docs/python-api.md`、`examples/api_client.py`、`examples/launch.json`、`examples/mixed-launch.json`。
- `ui/report-viewer.html` 本地载入 `report.json` 或 `candidates.csv`。
- 实现入口：`cli.py`、`orchestrator.py`、`scoring.py`、`aws/`、`probes/`、`reputation/`、`report.py`、`web/`。
