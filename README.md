# aws-crossborder-instance-selector

在指定 AWS 区域批量创建临时 EC2 实例，查询其公网 IPv4 的信誉名单记录，并执行网络探测。工具按本次测量结果计算得分，保留排名靠前的实例并终止其余实例。

结果用于比较本次候选，不代表长期网络质量、带宽或业务可用性。测量分两个方向：`agent`（客户中国区服务器 → 候选 EC2）与 Globalping 的 CN 探针提供 China → AWS 方向的主信号；反向探测（候选 EC2 → 大陆目标）只作为 AWS → China 回程健康度，默认权重 0.1。跨境路由通常非对称，两个方向不能互相推断。评分使用逐包 P95 时延与抖动，并融合同网段（prefix）的历史得分。

## 快速开始

需要 Python 3.11 或更新版本。真实运行还需要本机 AWS 凭证及相应权限。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest

# 仅查看计划，不调用 AWS API
scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run

# 本地模拟向导，不操作 AWS
scripts/start_web.sh --demo

# 真实运行：创建最多 2 台实例，测量 1 轮，保留 1 台
scripts/find_best_instance.sh ap-east-1 2 1 1

# 真实 Web 向导
scripts/start_web.sh
```

位置参数依次为区域、每轮候选数、最大轮次和保留数；额外选项写在这四个参数之后。真实运行会产生费用，保留实例会继续运行。

完整操作说明见 [MANUAL.md](MANUAL.md)，验证方法见 [TESTING.md](TESTING.md)。

## 工作流程

1. 读取配置，准备子网、安全组、SSM 实例配置和 Amazon Linux 2023 AMI。
2. 启动候选实例，为每台实例写入本次运行 ID 和轮次标签。
3. 查询 IP 信誉名单；命中名单的实例被排除，查询失败记录为未知状态。
4. 等待 SSM 注册，执行已启用的探测源。
5. 计算评分，将本轮候选与上一轮保留实例一起排序，保留前 K 台。
6. 达到最多轮次，或最高分达到目标值且保留数量已满足后，为保留实例写入 `crossborder-winner=true`。
7. 输出 JSON、Markdown 和 CSV 报告，以及 IP 网段历史统计。

候选实例的生命周期由本地进程管理。网络阻塞、进程强制退出或 AWS 清理请求失败可能留下实例；应记录终端输出的 `run-id`，通过清理命令核查。

## 探测源与网络要求

| 配置名 | 测量方式 | 方向 | 默认状态 | 限制 |
|---|---|---|---|---|
| `agent` | 客户中国区（或任意大陆）受 SSM 管理的服务器，经 SSM 向全部候选 IP 执行 ICMP ping 与 TCP 连接，输出逐包 RTT 与逐次连接耗时 | China → AWS | 关闭，需配置 `backends.agent.instances` | 只代表 agent 所在网络出口；每台 agent 的 isp 标签由配置给出，工具不校验 |
| `reverse` | 候选 EC2 经 SSM 向配置中的大陆目标执行 ICMP ping 和 TCP 连接测试 | AWS → China | 启用（权重 0.1） | 目标是有限的公共地址和域名（部分为 anycast），只反映回程健康度 |
| `globalping` | 香港、台湾、大陆公共探针向候选 IP 发送 ping，解析逐包 timings 得到 P95 与抖动 | 东亚/CN → AWS | 启用 | CN 探针数量少且多为数据中心出口，不代表住宅宽带；服务限额以提供方当前规则为准 |
| `ripeatlas` | RIPE Atlas 在中国大陆的探针向候选 IP 发送 ping | China → AWS | 关闭 | 需 API key、credits 和可用探针；需显式启用 |
| `itdog` | 配置中的公共节点向候选 IP 发送 ping | China → AWS | 关闭 | 非官方接口；节点 ID 与运营商映射需要维护，未验证节点为住宅宽带 |

### 客户侧部署 China → AWS 探针（agent）

`agent` 不需要客户开放任何入站端口，也不需要额外的上报服务：选择器通过 SSM `SendCommand` 把探测脚本下发到客户在中国区的服务器上执行，脚本只做出向 ping 和 TCP 连接，结果随命令输出返回。客户只需满足三点：

1. 服务器受 SSM 管理：Amazon Linux 2023 / Ubuntu 官方镜像自带 SSM Agent，实例挂有含 `AmazonSSMManagedInstanceCore` 的实例角色，且能访问 SSM 端点（公网出口或 VPC 端点）。`aws ssm describe-instance-information` 中该实例 `PingStatus` 为 `Online` 即可。
2. 运行选择器的一方对该账户有 `ssm:SendCommand`、`ssm:GetCommandInvocation`、`ssm:DescribeInstanceInformation` 权限。中国区是独立分区，通常用单独的 profile。
3. 服务器上有 `ping`、`bash`、`timeout`、`date`；AL2023 与 Ubuntu 默认满足。

配置示例（`config.yaml`）：

```yaml
backends:
  agent:
    enabled: true
    profile: cn                 # 中国区凭证 profile；留空走默认凭证链
    region: cn-north-1          # agent 实例所在区域
    instances:                  # instance_id -> isp 标签；标签用于三网加权，也可写自由文本
      i-0abc1234567890def: telecom
      i-0fed0987654321cba: unicom
    ping_count: 10
    tcp_ports: [443]            # 建议填业务实际端口
    tcp_count: 5
    timeout_s: 180
```

工作方式：每轮候选拿到公网 IP 后，选择器对每台 agent 实例下发一条命令，脚本内遍历全部候选 IP；单台 agent 失败只记为该轮警告，全部 agent 失败时候选按 `agent_unavailable` 否决，不会凭其他探测源保留。isp 标签为 `telecom` / `unicom` / `mobile` 时按 `weights.isps` 加权，其他标签等权平均。

真实客户场景下，客户在中国区已有的业务服务器就是最合适的 agent：它所在的网络出口就是业务流量真实经过的出口。用于试验时，可以用中国区账户临时启动一台最小实例充当 agent，用完终止。agent 脚本也可以在任何大陆 Linux 主机上手工执行（见 `crossborder_selector/probes/agent.py` 的 `build_agent_script`），输出以 `CROSSBORDER_AGENT_JSON:` 开头的一行 JSON。

局限：一台 agent 只代表一个出口。若要宣称"三网最优"，至少电信、联通、移动各一台；跨境网络里运营商差异通常大于城市差异。

仅启用反向探测时，工具使用无探测入站规则的 `crossborder-selector-sg`。启用任一外部探测源时，使用独立的 `crossborder-selector-ping-sg`，允许来自 `0.0.0.0/0` 的 IPv4 ICMP Echo Request（类型 8、代码 0），不开放 TCP/UDP 端口。公共探针地址会变化，因此此处不按固定探针 IP 限制来源。

安全组会跨运行复用，保留实例继续使用原安全组；清理实例不会自动撤销共享规则。显式指定安全组时，工具只验证其 VPC 和探测入站条件，不自动修改用户指定的组。外部探测条件不满足时，在启动实例前报错。网络 ACL、路由和目标自身行为仍可能影响结果。

`reverse.targets` 支持 `host` 或 `host:port`。ICMP 使用 host；TCP 使用显式端口或 `tcping_port`。示例中的 DNS 地址显式使用 53 端口。ICMP 显示平均往返时延；TCP 显示平均连接耗时，包含名称解析和本机执行开销。

## 评分与检查状态

每个样本得分为 `100 × (1 − 丢包率) × 延迟因子 × 抖动因子`。延迟因子取 P95 时延（探测源不提供逐包数据时退回平均值），在 `lat_good_ms` 和 `lat_bad_ms` 之间线性递减；抖动因子在抖动达到 `jitter_bad_ms` 时扣满 `jitter_penalty`，无抖动数据不扣。同一探测源内先合并运营商样本，再按探测源权重计算本次综合分（`instant_composite`）。只有返回有效测量数据的探测源参与权重归一化；不同覆盖范围下的得分应结合明细比较。

最终用于排序的 `composite = (1 − prefix_history) × instant_composite + prefix_history × 同网段历史均分`。历史来自 `history/prefix_stats.json`，只在该网段样本数达到 `prefix_min_samples` 时融合，报告中 `prefix_history_score` 记录所用的历史分。融合的目的：单次几十个包的测量噪声大，同一 /16 或 /15 的过去表现更稳定；下一次抽到同网段 IP 时不必依赖大样本重测。

以下情况会排除候选：

- 未分配公网 IP，或命中所查询的信誉名单。
- 已启用反向探测，但该探测失败、未覆盖全部配置运营商，或所有目标均未响应。
- 已启用 `agent`，但没有任何 agent 实例返回有效样本（`agent_unavailable`）。
- 有效探测源少于 `min_backends`。
- 所有有效测量均没有成功响应。

完成但全部丢包的测量仍用于显示丢包数据，不会单独证明实例可用。排序依据为合格状态、综合分、反向探测分。IP 网段仅用于统计，不参与排序。

信誉检查状态为 `clear`（所查名单未命中）、`listed`（命中）或 `unknown`（检查未完成）。查询失败时不提供完整信誉分。GitHub 名单检查失败默认排除候选（`reputation.require_badlist: true`）；其他信誉源失败仍记录为未知；“未命中”不是 IP 无风险或业务平台可接受该 IP 的保证。

## 配置

```bash
cp config.example.yaml config.yaml
```

CLI 和 Web 服务自动读取当前目录中的 `config.yaml`，也可用 `--config` 指定文件。

| 配置 | 默认值 | 说明 |
|---|---|---|
| `region` / `instance_type` | `ap-east-1` / `t3.nano` | 区域和机型 |
| `batch_size` / `max_rounds` / `keep_top_k` | `10` / `3` / `1` | 每轮数量 1–50、轮次 1–10、保留数 1–50；保留数不能超过计划启动总数 |
| `target_score` | `90` | 最高综合分达到此值可提前停止 |
| `min_backends` | `1` | 有效探测源的最低数量，不得超过实际可用的已启用源数量 |
| `protect` | `false` | CLI 在筛选结束后、Web 在人工选定后启用停止和终止保护 |
| `weights.backends` | agent .4 / globalping .3 / reverse .1 / ripeatlas .1 / itdog .1 | 非负权重；已启用源须有正权重。reverse 是回程信号，不应主导 |
| `weights.isps` | 电信 .34 / 联通 .33 / 移动 .33 | 非负权重，总和须大于 0 |
| `weights.lat_good_ms` / `lat_bad_ms` | `60` / `300` | 延迟因子的端点，作用于 P95（无逐包数据时为平均值） |
| `weights.jitter_bad_ms` / `jitter_penalty` | `50` / `0.3` | 抖动达到 `jitter_bad_ms` 时扣满 `jitter_penalty`，0 表示不扣 |
| `weights.prefix_history` / `prefix_min_samples` | `0.3` / `3` | 网段历史在最终分中的权重（须小于 1）与最低样本数 |
| `backends.globalping.locations` / `limit_per_location` | `HK, TW, CN` / `3` | 同城探针位置差异可达一倍，每地区至少 3 个探针 |
| `backends.agent.*` | 关闭 | 见"客户侧部署 China → AWS 探针" |
| `subnet_id` / `security_group_id` / `instance_profile_name` | 空 | 未提供时准备或复用工具管理的资源 |

RIPE Atlas 需要同时设置 `backends.ripeatlas.enabled: true` 和 `api_key`。也可通过 `--enable-backend ripeatlas` 启用。密钥字段及已知密钥在探测错误中的值会在报告和 Web 运行日志中脱敏。

## Web 向导

```bash
scripts/start_web.sh --port 8765
scripts/start_web.sh --demo --no-browser
```

向导提供配置、确认、运行和结果四个阶段，日志和诊断默认折叠。区域列表包含 SDK 已知区域和 AWS 返回的区域，并支持手动输入区域代号；账户未启用的区域及其他分区需要对应账户权限与凭证。机型名称来自 AWS 完整分页结果，选中后查询 CPU、内存和架构；指定子网后按其可用区筛选。查询过程中显示加载状态，失败后可重新加载，同一区域和子网已有的结果会标明为上次查询结果。默认子网也会选择支持该机型的可用区。机型列表不承诺实时容量。

每轮启动数量可以配置为 1–50 台，轮次为 1–10；启动 API 要求完整数量，容量不足不会静默创建更少实例。配额检查按所选机型的真实 vCPU 和对应 On-Demand 配额组计算，排除 Spot，用量包含跨轮保留实例。配额无法读取或已有实例规格无法确认时，不应把页面上的未知值当作可用额度保证。

默认监听 `127.0.0.1`。使用 `--host <address> --allow-remote` 可以启用远程访问；服务没有身份认证，可访问端口的用户能够操作实例，需要限制网络来源。POST 请求验证协议、主机和端口一致的 Origin；这不是身份认证。

取消会请求在当前轮结束后停止，保留已测实例。关闭浏览器不停止服务；关闭服务进程则可能留下实例。重启后，未完成记录显示“状态未知”，可按该运行 ID 查询并清理临时候选。

## 镜像与系统盘

开始页的“添加机型”支持多行配置，例如 `t3.nano × 2` 和 `t4g.nano × 3`。每行设置“每轮启动台数”，页面自动显示每轮总数；“最终希望保留”独立设置所有机型合计的保留目标。每轮最多 50 台，保留数为 1–50 台且不能超过计划启动总数。未凑够保留数量时，即使已有候选达到评分目标，也会继续下一轮，直到数量满足或达到最多轮次；合格候选不足时最终数量可能少于目标。调整某行数量或移除机型时，其他机型的逐台配置保持对应。

API 和配置文件使用 `instance_groups: [{instance_type: t3.nano, count: 2}, {instance_type: t4g.nano, count: 3}]`。非空清单决定 `batch_size` 和实际各机型数量；空清单保留原 `instance_type` + `batch_size` 用法。配额按对应的 vCPU 配额组汇总，并为跨轮可能保留的最大 vCPU 用量预留空间。默认 VPC 中允许为不同机型选择不同可用区的子网；指定子网时，每一种机型都必须在该可用区提供。

页面提供 Amazon Linux 2023、Ubuntu 24.04 LTS 和 Ubuntu 22.04 LTS 选项，按当前区域和机型架构查询实际 AMI，也可手动输入 AMI ID。切换区域或架构后，已选系统会重新解析；无法获取时提示重新选择，不自动换成其他系统。根卷容量、卷类型和加密可配置；“逐台设置”可覆盖每轮指定序号候选的镜像、容量和卷类型。配置文件示例：

```yaml
image_id: ''                 # 自动匹配架构的 Amazon Linux 2023
root_volume_size_gib: 20
root_volume_type: gp3        # gp3、gp2、standard
root_volume_encrypted: true
instance_overrides:
  - {}                      # 第 1 台继承默认值
  - image_id: ami-0123456789abcdef0  # 替换为当前区域可用的 AMI
    root_volume_size_gib: 40
```

启动前验证 AMI 可见性、状态、架构和 EBS 根卷最小大小，并使用镜像实际根设备名。根卷随实例终止删除；没有覆盖的其他镜像设备映射遵循 AMI 本身的设置。自定义 Linux 镜像需有可用的 SSM Agent；Windows 镜像不能执行当前反向探测脚本。需要 Dedicated Host 等额外启动参数的机型，仍需要相应部署条件；当前工具没有专用主机配置项。

不同配置分组启动。后续分组失败时，工具尝试终止本轮之前已创建的实例，保留运行标签以支持清理。JSON 报告记录每个候选实际下发的镜像和根卷设置。

混合 ARM 和 x86 机型时，可用 `image_preset: ubuntu2404` 按每台架构解析镜像，也可在 `instance_overrides` 中逐台指定 `image_preset` 或 `image_id`。手工填写的同一 AMI 不会自动转换架构；不兼容时在启动前报错。完整例子见 [mixed-launch.json](examples/mixed-launch.json)。

## Python 与 Skill

[Python 使用方法](docs/python-api.md) 提供标准库 API 客户端、镜像查询、逐台配置和启动示例：

```bash
python3 examples/api_client.py images --region ap-east-1 --instance-type t4g.nano
python3 examples/api_client.py plan --config-json examples/launch.json
```

无参数运行脚本默认查看计划。实际启动使用 `start --execute`。可复用的 [crossborder-select Skill](.claude/skills/crossborder-select/SKILL.md) 包含 Python、Web、CLI 工作流程和资源状态处理说明。

## GitHub IP 名单

默认查询 [nginx-ultimate-bad-bot-blocker 的 IP 名单](https://github.com/mitchellkrogza/nginx-ultimate-bad-bot-blocker/blob/master/_generator_lists/bad-ip-addresses.list)。该仓库维护拦截名单，命中只代表该维护者的分类。

可在高级设置或 `reputation.badlist_url` 指定 HTTPS 原始文本文件。支持 IP、CIDR、注释和 IP 后的空白分隔字段。每次运行加载一次；空文件、HTML 网页、无效条目和下载失败都记录为错误。默认 `require_badlist: true` 排除检查失败的候选；成功检查后，命中即排除。结果卡片显示 GitHub 名单状态，JSON 明细保留源地址及命中项。

## 资源归属与清理

```bash
.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id>
```

新版本保留实例上的 `crossborder-run-id`，用 `crossborder-winner=true` 标记保留状态。清理按运行归属查询，并排除已标记为保留的实例；旧版本保留实例的 `crossborder-source-run` 标签也可被识别。

`--include-infra` 只额外删除当前区域的工具受管安全组，共享 IAM 角色和实例配置默认保留。删除账户级 IAM 资源需再加 `--include-iam`；工具检查所有权、其他实例配置及所有已启用区域中的实例依赖。任何依赖查询失败都会停止 IAM 删除。清理共享资源期间应停止发起新的筛选运行。

Web 的“终止其余保留候选”用于清理人工选定后不再需要的保留实例，并持久化操作结果。它与普通 `cleanup --run-id` 的范围不同。

## 运行前提与权限

目标区域需要可用子网和出网路径。没有默认 VPC 时指定 `subnet_id`；安全组可显式提供或由工具准备。vCPU 配额应覆盖已有实例、本轮候选和上一轮保留实例，不能只按候选数量计算。

日常操作涉及：`ec2:RunInstances`、`ec2:Describe*`、`ec2:TerminateInstances`、`ec2:CreateTags`、`ec2:CreateSecurityGroup`、`ec2:AuthorizeSecurityGroupIngress`、`ec2:ModifyInstanceAttribute`、`iam:CreateRole`、`iam:AttachRolePolicy`、`iam:CreateInstanceProfile`、`iam:AddRoleToInstanceProfile`、`iam:TagRole`、`iam:TagInstanceProfile`、`iam:PassRole`、`iam:Get*`、`ssm:SendCommand`、`ssm:GetCommandInvocation`、`ssm:DescribeInstanceInformation`、`ssm:GetParameters`。Web 检查还使用 STS 身份查询、`servicequotas:GetServiceQuota` 和 `servicequotas:ListServiceQuotas`。

安全组清理需要 `ec2:DeleteSecurityGroup`。账户级 IAM 清理还需要 `ec2:DescribeRegions`、各区域的 `ec2:DescribeInstances`、`iam:ListInstanceProfilesForRole`、`iam:RemoveRoleFromInstanceProfile`、`iam:DeleteInstanceProfile`、`iam:DetachRolePolicy`、`iam:DeleteRole`。这是一份调用清单，授权策略的资源范围需按账户配置。

工具不复制操作者的长期密钥到实例。实例通过关联的 IAM 角色取得 SSM 所需的临时凭证。

## 费用与保留实例

Web 展示基于候选数量、启用探测源、超时配置和跨轮保留时间的规划范围，不是运行时长上限。香港已列机型使用项目内静态参考价，未实时核价；其他区域或未知机型不套用香港价格。

显示的费用只包括 EC2 和公网 IPv4 参考费用，未包含 EBS 根卷、流量和最终保留实例的后续费用。实际费用以所用区域、机型和运行时间为准。

保留实例仍是普通 EC2 实例。stop/start 会更换自动分配的公网 IPv4，reboot 通常保留该地址。实例内发起关机的行为设为 stop。API 停止和终止保护不阻止操作系统内关机；设置失败会明确报错。

工具不安装代理或业务软件。部署应用、开放服务端口、访问控制及长期监控需另行配置。

## 报告与项目结构

`out/<run-id>/report.json` 保存测量快照、评分、信誉状态、探测样本及错误；`report.md` 用于阅读，`candidates.csv` 用于表格分析。`selection.json` 保存 Web 后续选定和终止操作，界面据此更新资源状态。报告本身不代表当前 AWS 实例状态。

`ui/report-viewer.html` 可在浏览器本地载入 JSON 或 CSV。CSV 不包含完整探测明细，诊断应使用 JSON。`.claude/skills/crossborder-select/SKILL.md` 提供 Claude Code 的命令调用说明。

实现入口：`cli.py`（命令行）、`orchestrator.py`（多轮筛选）、`scoring.py`（评分）、`aws/`（资源与 SSM）、`probes/`（探测源）、`reputation/`（名单检查）、`report.py`（报告）、`web/`（向导）。`docs/superpowers/` 保存历史设计和实施记录，当前行为以代码及本 README、操作手册为准。
