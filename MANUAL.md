# 操作手册

## 安装与配置

全新机器请先按 [README 的"从零开始"](README.md#从零开始一台全新机器) 装好 Python 3.11+、git、AWS CLI 并配置凭证。

```bash
git clone https://github.com/ButterflyJuicer0/aws-crossborder-instance-selector.git
cd aws-crossborder-instance-selector
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
export AWS_PROFILE=<name>     # 真实运行使用的凭证；select/cleanup 没有 --profile 参数
```

先设置区域、机型和探测规模。没有默认 VPC 时提供 `subnet_id`；自定义安全组必须与子网属于同一 VPC。外部 ping 需要 IPv4 ICMP Echo Request 入站规则，详见 [README](README.md#探测源与网络要求)。

CLI 参数覆盖配置文件中的对应值。包装脚本默认填入数量和轮次；希望完整使用文件配置时，可直接运行 `.venv/bin/python -m crossborder_selector.cli select --config config.yaml`。

## 查看计划

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run
```

dry-run 只读取配置和生成文本，不访问 AWS，也不会核验凭证、权限、配额或网络。检查区域、实例数量、探测源、目标地址和入站要求后再真实运行。

## 小规模真实运行

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1
```

记录输出的 `run-id`。完成后检查 `out/<run-id>/report.md` 和 `report.json`：候选总数、保留实例、停止原因、每个探测源的丢包率与错误，以及信誉检查是否完成。

运行时间随实例启动、SSM、信誉服务和探测源响应变化。未达到目标分时仍可能保留满足最低测量要求的实例；得分最高不等于满足业务要求。

## 自定义镜像和系统盘

```bash
.venv/bin/python -m crossborder_selector.cli select --region ap-east-1 \
  --batch-size 2 --max-rounds 1 --root-volume-size-gib 30 --root-volume-type gp3
```

指定镜像可增加 `--image-id <ami-id>`；逐台覆盖通过网页或 `config.yaml` 的 `instance_overrides` 配置。自定义 AMI 必须属于所选区域、架构匹配且根卷容量满足要求。高级设置中的子网必须有出网路径。

## 多轮运行与保护

```bash
scripts/find_best_instance.sh ap-east-1 20 3 1 --protect
```

需要为本轮候选和上一轮保留实例同时预留配额。`--protect` 在 CLI 筛选结束后启用停止和终止保护。任一保护设置失败会报告错误，不表示两项均已成功。

Ctrl-C 中断时退出码为 130，按输出的运行 ID 清理未保留实例。强制终止进程或清理 API 失败可能留下资源。

## Web 向导

```bash
scripts/start_web.sh
scripts/start_web.sh --demo
scripts/start_web.sh --port 8792 --no-browser
```

默认地址为 `http://127.0.0.1:8765`。模拟模式不调用 AWS，适合先检查页面和操作流程。真实模式使用本机 AWS 凭证。

环境检查允许先确认基本前提、再调整规模。点击开始时，按最终配置再次检查 vCPU 配额，包括已知占用和跨轮保留实例。未知占用或配额不等于资源一定可用。机型即使在列表中，也可能因为容量、AMI、权限或专用主机要求而无法启动。

在配置页选择或手动输入区域代号，再从 AWS 机型列表搜索机型。名称先加载，选中后查询规格。区域和机型列表可滚动；输入文字筛选，方向键选择、Enter 确认，Escape 收起。若列表加载失败，点击“重新加载机型”重试；刷新不会清除已填写的机型、台数和保留数量。

点击“添加机型”可增加多行，每行分别选择机型并设置“每轮启动台数”。主表单中的“每轮启动总数”自动合计，“最终希望保留”可独立设置为 1–50 台，且不能超过每轮总数乘最多轮次。例如每轮 6 台、最多 2 轮、希望保留 7 台，会从最多 12 台候选中选取 7 台；合格候选不足时可能少于 7 台。达到评分目标但数量不足时会继续后续轮次。

“检查清单配额”按所有行合计用量，每轮总数最多 50 台。逐台配置按机型行顺序展开；调整前一行数量不会将后一行已有的镜像和磁盘设置移给其他机型。

“系统镜像”提供按区域和架构匹配的 Amazon Linux 2023、Ubuntu 24.04 LTS、Ubuntu 22.04 LTS；选择后显示真实 AMI ID 和最小系统盘容量。其他镜像选择“手动输入 AMI ID”。默认镜像和系统盘设置适用于每台实例；需要差异时展开“逐台设置镜像与系统盘”，每台也可选择常用系统。Python 调用见 [Python 使用方法](docs/python-api.md)。

选定实例时可分别决定是否启用保护、是否终止其他保留实例。Web 的保护在人工选定时设置。终止实例不可恢复；操作结果会写入 `selection.json`，刷新后仍可查看。

取消请求在轮次边界生效。浏览器关闭后服务仍可运行；服务进程退出后，未完成运行在历史中显示“状态未知”。点击“查询并清理本次候选”按运行 ID 查询 AWS 并清理未标记为保留的实例。

`--host <address> --allow-remote` 可启用远程访问。服务没有身份认证，应限制网络来源；同源 POST 的协议、主机和端口必须一致。`--allow-remote` 不等于关闭跨站检查。

## 启用客户侧 agent 探测（China → AWS）

agent 是默认权重最高的探测源。agent 可以是客户任意一台能出网的机器（物理机、其他云、本地电脑），也可以是中国区受 SSM 管理的 EC2。

### 任意机器（http 传输）

1. 在 `config.yaml` 启用并设 token：

```yaml
backends:
  agent:
    enabled: true
    transport: http
    http: {listen: 0.0.0.0:8766, token: 'change-me'}   # 仅本机测试可保持 127.0.0.1 且不设 token
    min_agents: 1
    tcp_ports: [443]
```

2. 把 `agent/crossborder_agent.py` 复制到客户机器，先验证再常驻：

```bash
python3 crossborder_agent.py once --targets 8.8.8.8 --ports 443          # 本机探测能力自检
python3 crossborder_agent.py serve --server http://<选择器地址>:8766 --token change-me \
  --agent-id bj-telecom-01 --isp telecom --once                          # 领一次任务验证连通
nohup python3 crossborder_agent.py serve --server http://<选择器地址>:8766 --token change-me \
  --agent-id bj-telecom-01 --isp telecom >agent.log 2>&1 &              # 常驻
```

3. 运行选择器。CLI 会在 `http.listen` 起监听器；Web 向导用 Web 端口本身（agent 的 `--server` 指向 Web 地址即可），页面"探测源"下能看到已连接的 agent。监听地址对外暴露时用防火墙限制来源 IP。

### 任意机器但选择器不可达（s3 传输）

选择器侧 `transport: s3`、`s3.bucket` 填 bucket，`profile`/`region` 指向 bucket 所在账户；agent 侧 `serve --s3 s3://<bucket>/crossborder-agent --agent-id ... --isp ... --profile <凭证>`，需要 boto3。给 agent 的凭证只授予该前缀的读写。

### 中国区 SSM 实例（ssm 传输）

1. 确认服务器 SSM 在线（用中国区凭证）：

```bash
aws ssm describe-instance-information --profile cn --region cn-north-1 \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus}' --output table
```

2. 在 `config.yaml` 填入实例与其运营商标签：

```yaml
backends:
  agent:
    enabled: true
    profile: cn
    region: cn-north-1
    instances:
      i-0abc1234567890def: telecom
    tcp_ports: [443]
```

3. 先 dry-run 核对计划中出现 `agent`，再真实运行。报告中每个候选会多出 `agent_telecom` 等列，`probe_results` 里 `backend: agent` 的样本含 `p95_rtt_ms` 与 `jitter_ms`。

agent 只做出向 ping 和 TCP 连接，不需要客户开放入站端口。单台 agent 失败在报告 `warning` 中体现；全部失败时候选按 `agent_unavailable` 否决。没有客户服务器时可用中国区账户临时启动一台最小实例充当 agent，用完终止。

## 启用可选探测源

RIPE Atlas 需要 API key 和 credits，并显式启用：

```yaml
backends:
  ripeatlas:
    enabled: true
    api_key: '<your-key>'
```

也可先配置 key，再使用 `--enable-backend ripeatlas`。仅填写 key 不会自动启用。一次性测量的结果可能延迟到达；`timeout_s` 决定等待窗口，部分结果需结合覆盖范围判断。

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1 --enable-backend itdog
```

itdog 使用非官方接口。HTTP 请求具有连接和读取超时；WebSocket 短暂空闲会继续等待至收集窗口结束。错误会记录到报告，其他探测源可继续提供数据。

## 清理

```bash
.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id>
```

清理识别本次运行归属，并排除 `crossborder-winner=true` 的实例。新版本的保留实例也保留运行 ID 标签，因此不能用“按运行 ID 查询是否为空”判断清理是否完成。

```bash
# 额外删除当前区域的受管安全组，共享 IAM 默认保留
.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id> --include-infra

# 仅在确认不再需要共享资源时使用；工具检查所有已启用区域及 IAM 所有权
.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id> --include-infra --include-iam
```

若存在保留实例，工具拒绝基础设施清理。安全组仍被正在关闭的实例引用时，等待后重试。其他区域仍有实例依赖、存在其他实例配置依赖、所有权不符或查询失败时，IAM 资源不会删除。清理共享资源期间不要并行开始新的运行。

Web 已保留但最终未选定的实例，应使用“终止其余保留候选”；普通 cleanup 会排除这些实例。

## 排查

| 现象 | 检查内容 |
|---|---|
| `agent_unavailable` | ssm：实例是否 SSM Online、`profile`/`region` 是否正确；http：agent 是否指向正确地址与 token（页面 registry 或 `GET /api/agent/registry` 能否看到它）、`timeout_s` 是否够 agent 完成一轮；s3：bucket/prefix 与凭证；报告 `probe_results` 中 agent 条目的 error |
| `reverse_unavailable` | SSM 注册状态、实例角色、出网路径和报告中的具体错误 |
| `reverse_incomplete` | 反向探测是否返回全部配置运营商的样本 |
| `reverse_unreachable` / `unreachable` | 配置目标是否响应，外部探测的安全组、网络 ACL 和路由是否允许；不要直接认定整个运营商网络不可达 |
| `min_backends` | 有效探测源数量、API 错误和结果覆盖 |
| `reputation_unavailable` | GitHub 原始名单下载或解析失败；核查 URL 和错误详情，默认不保留这类候选 |
| 信誉 `unknown` | JSON 中 `reputation_results` 的 error/detail；区分 DNS 超时、SERVFAIL 和名单提供方错误码 |
| 信誉 `listed` | 检查实际命中的源和名单；不是查询失败 |
| Globalping 429 | 查询提供方当前限额，减少数量或配置有可用额度的 token |
| RIPE Atlas 未启用 | 同时检查 enabled 与 api_key |
| RunInstances 配额/容量错误 | 工具不针对这类错误主动缩批重试；减少规模或处理配额/容量后重新运行 |
| 保护设置失败 | 在 EC2 检查停止与终止保护各自状态；修复权限后重试 |
| Web 状态未知 | 未完成残留查询不代表没有实例；使用按运行 ID 清理功能 |
| 报告写入失败 | 检查输出目录与磁盘；先核对已标记的保留实例和残留候选 |

## 使用保留实例

自动分配的公网 IPv4 不能迁移为 EIP。stop/start 会更换地址；reboot 通常保留。实例内发起关机设为 stop，API 保护不阻止这种关机。

部署业务前另行配置应用、服务端口和访问控制。工具的网络分数不能替代真实业务测试或持续监控。

```bash
# 如需解除保护，使用该实例实际所属区域
aws ec2 modify-instance-attribute --region <region> --instance-id <id> --no-disable-api-stop
aws ec2 modify-instance-attribute --region <region> --instance-id <id> --no-disable-api-termination
```

删除 `crossborder-winner` 标签会改变普通 cleanup 对该实例的处理：仍有运行归属标签的实例将重新成为可清理对象。不要仅为整理标签而删除该保留标记。
