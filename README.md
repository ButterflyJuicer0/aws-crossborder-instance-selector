# AWS 跨境优选实例选择器（crossborder-selector）

## 1. 背景：为什么保留实例而不是 EIP

AWS 没有"跨境优选 IP"服务。EC2 自动分配的公网 IPv4 无法转成 EIP；EIP 分配器会反复返回同一地址，且每 Region 仅 5 个配额，无法用来"抽好 IP"。因此最可靠的做法是：批量启动临时 EC2，从中国大陆视角拨测其自动公网 IPv4，保留最优实例、终止其余。只要该实例持续运行，这个 IP 就一直可用——reboot 保留 IP，stop/start 会更换 IP。

## 2. 一句话用法

```bash
scripts/find_best_instance.sh ap-east-1 20 3 1           # region batch rounds keep
scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run  # 只打印计划，不创建资源、不需凭证
```

参数依次为 region、每轮候选数、最大轮次、保留数；其后可追加任意 CLI 参数（如 `--protect`、`--enable-backend itdog`）。

## 3. 流程图

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
 │         itdog      三网家宽视角 ping 候选 IP                   显式开启才用
 │    ⑤ 打分 → 与在位 winner 合并 → 全局 Top-K 保留，其余终止
 │    ⑥ best_score >= target_score 或 round == max_rounds → 停止
 ├─ winner 处理：删除 crossborder-run-id 标签；打 crossborder-winner=true、
 │    crossborder-score、crossborder-round、crossborder-selected-at 标签
 └─ 输出 out/<run-id>/report.json、report.md、candidates.csv；
      追加 history/prefix_stats.json
```

## 4. 拨测数据源

| backend | 视角 | 默认开关 | 需要 key | 局限 |
|---|---|---|---|---|
| reverse | 候选机 → 大陆三网机房（出境近似） | 开 | 否 | 探测机房而非家宽，ICMP 可能被限速；建议 finalists 用 itdog 复核 |
| globalping | HK/TW 公共探针 → 候选 IP | 开 | 否 | 无中国大陆探针，只反映港台质量 |
| ripeatlas | 大陆在线探针 → 候选 IP | 关 | 是 | 大陆在线探针数量少，覆盖有限 |
| itdog | 三网家宽 → 候选 IP（仅 ping） | 关 | 否 | 非官方接口，随时可能失效；只做 ping 不做 tcping；仅失败降级不阻塞 |

reverse 的 `targets` 每项可写 `host` 或 `host:port`：ping 只用 host 部分，tcping 用条目端口、未给则回退到 `tcping_port`。默认三网目标里纯 IP 是运营商公共 DNS（走 53），域名走 `tcping_port`（443），避免对 DNS 服务器 tcping 443 得到结构性零分。

globalping 公共 API 匿名限速约 250 tests/h；在 `backends.globalping.api_token` 填入 token 后额度提升到约 500 tests/h（token 会在报告中脱敏）。

## 5. 打分

先做硬否决：信誉命中、reverse 三网全丢包、或有效 backend 数 < `min_backends`，任一成立即淘汰。子分按每个探针计算 `100 × (1 − 丢包率) × 延迟因子`，延迟因子在 `lat_good_ms`~`lat_bad_ms` 间线性衰减。backend 分先按三网权重 `weights.isps` 合并，composite 再按 `weights.backends` 加权，只对返回有效数据的 backend 归一化，缺失不计零分。排序键为 `(qualified, composite, reverse 分)` 降序。prefix 只记录、不参与打分。

## 6. 前置条件

- AWS 凭证需具备 EC2 / IAM / SSM 权限，最小 action 清单：`ec2:RunInstances`、`ec2:Describe*`、`ec2:TerminateInstances`、`ec2:CreateTags`、`ec2:DeleteTags`、`ec2:CreateSecurityGroup`、`ec2:ModifyInstanceAttribute`、`iam:CreateRole`、`iam:AttachRolePolicy`、`iam:CreateInstanceProfile`、`iam:AddRoleToInstanceProfile`、`iam:TagRole`、`iam:TagInstanceProfile`、`iam:PassRole`、`iam:Get*`、`ssm:SendCommand`、`ssm:GetCommandInvocation`、`ssm:DescribeInstanceInformation`、`ssm:GetParameters`。
- 仅 `cleanup --include-infra` 删除共享基础设施时额外需要：`ec2:DeleteSecurityGroup`、`iam:RemoveRoleFromInstanceProfile`、`iam:DeleteInstanceProfile`、`iam:DetachRolePolicy`、`iam:DeleteRole`。
- 目标 Region 存在默认 VPC（否则在 `config.yaml` 手工指定 `subnet_id` 与 `security_group_id`）。
- 该 Region vCPU 配额 ≥ `batch_size × 2`，避免 RunInstances 被限额缩批。

## 7. 成本

香港 t3.nano 加公网 IPv4 每台每小时约 0.012 美元，每轮候选机存活 5～8 分钟；20 台 × 3 轮总成本低于 0.5 美元。胜出实例长期运行按机型正常计费，公网 IPv4 每小时 0.005 美元。

## 8. winner 注意事项

- 不要 `stop` 该实例：stop/start 会更换公网 IP，reboot 才保留 IP。
- winner 的 `InstanceInitiatedShutdownBehavior` 已由默认 `terminate` 改为 `stop`，即使在 OS 内执行 `shutdown`/`poweroff` 也只会停机而不会终止实例（避免丢失该 IP）。
- 标签含义：`crossborder-winner=true`、`crossborder-score`（综合分）、`crossborder-round`（胜出轮次）、`crossborder-selected-at`（选定时间）；胜出后 `crossborder-run-id` 标签会被移除，故不受 `cleanup --run-id` 影响。
- 加 `--protect` 会对 winner 开启 `DisableApiStop` 与 `DisableApiTermination`；手动解除：`aws ec2 modify-instance-attribute --instance-id <id> --no-disable-api-termination`（stop 保护同理用 `--no-disable-api-stop`）。

## 9. 安全边界

- 安全组 `crossborder-selector-sg` 无任何入站规则，候选机不开放任何端口、不配置 SSH。
- 全程不向候选机注入任何 AWS 凭证；探测经 SSM 下发只读脚本完成。
- 所有候选机按 `crossborder-run-id` 打标签隔离，`cleanup --run-id` 只影响本次 run。

## 10. 局限与非目标

- 不做 EIP 筛选（姊妹项目 `clean-ip-selection` 已覆盖）。
- 不做长期监控与自动换机。
- 不做基于 prefix 历史的自动跳过（仅记录，留作后续扩展）。
- 不接入任何需付费或国内云账号的拨测 API。

详细操作步骤见 [MANUAL.md](MANUAL.md)。
