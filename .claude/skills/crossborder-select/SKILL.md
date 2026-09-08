---
name: crossborder-select
description: 在 AWS 上批量启动候选 EC2、从中国大陆视角拨测其公网 IPv4 并保留最优实例。用于用户提出"找一台跨境网络质量好的 EC2"、"筛选跨境优选 IP 的机器"、"运行 crossborder selector"、"清理某次 run 的候选机"、"看某次 run 的报告"等请求。
---

# crossborder-select

本 skill 驱动仓库 `aws-crossborder-instance-selector` 的命令行工具。工具不依赖任何 AI 模型，全部逻辑是确定性的：boto3 管理 EC2，SSM 下发探测脚本，公开 REST API 做辅助拨测，固定公式打分。

## 前置检查（每次运行前都做）

1. 确认工作目录是仓库根：存在 `crossborder_selector/` 与 `scripts/find_best_instance.sh`。
2. 确认虚拟环境可用：`ls .venv/bin/python`。不存在时执行 `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`。
3. 确认 AWS 凭证与 Region：`aws sts get-caller-identity` 与 `aws ec2 describe-vpcs --filters Name=isDefault,Values=true --region <region>`。没有默认 VPC 时要求用户在 `config.yaml` 填 `subnet_id` 与 `security_group_id`。
4. 先跑 dry-run 再跑真实命令。dry-run 不创建资源、不需要凭证。

## 命令速查

| 目的 | 命令 |
|---|---|
| 查看计划 | `scripts/find_best_instance.sh <region> <batch> <rounds> <keep> --dry-run` |
| 小规模冒烟 | `scripts/find_best_instance.sh <region> 2 1 1` |
| 正式运行 | `scripts/find_best_instance.sh <region> 20 3 1 --protect` |
| 启用可选 backend | 追加 `--enable-backend itdog`（ripeatlas 需先在 `config.yaml` 填 `api_key`） |
| 清理某次 run | `.venv/bin/python -m crossborder_selector.cli cleanup --region <region> --run-id <run-id>` |
| 重生成报告 | `.venv/bin/python -m crossborder_selector.cli report --run-id <run-id> --output-dir ./out` |
| 单元测试 | `. .venv/bin/activate && pytest -q` |

参数依次为 Region、每轮候选数、最大轮次、保留数。默认 Region 为 `ap-east-1`。

## 执行流程

1. 与用户确认 Region、每轮候选数、轮次、是否开启 `--protect`。用户没说时用 `ap-east-1 10 3 1`，并说明每轮候选机存活约 5～8 分钟，20 台 × 3 轮成本低于 0.5 美元。
2. 运行 dry-run，把输出原样展示给用户，确认 backend 列表与探测目标。
3. 运行真实命令。命令输出第一行是 `run-id: xb-...`，立即记下并告知用户，这是清理和查报告的唯一凭据。
4. 命令结束后读取 `out/<run-id>/report.md`，向用户汇报：winner 的 instance id、公网 IP、prefix、综合分与三网分项；停止原因；每轮淘汰数量；`backend_errors`。
5. 提醒用户：winner 不能 stop（stop/start 会更换公网 IP，reboot 不会）；winner 已移除 `crossborder-run-id` 标签，`cleanup` 不会影响它；关机行为已设为 stop。
6. 若用户中断（Ctrl-C，退出码 130），输出里会列出遗留实例 id；立即运行 `cleanup --run-id <run-id>` 并用 `aws ec2 describe-instances --filters Name=tag:crossborder-run-id,Values=<run-id>` 确认为空。

## 结果解读

- `stop_reason`：`target_score_reached` 达到目标分；`max_rounds` 轮次用尽；`no_qualified` 没有合格候选；`launch_failed` 后续轮次启动失败但保留了在位 winner。
- `veto_reason`：`reputation` 黑名单命中；`reverse_unreachable` 三网全部丢包；`min_backends` 有效 backend 不足；`no_public_ip` 未分配公网 IP。
- 全部候选都是 `reputation` 且 dnsbl detail 为 `error:...`：本机使用公共递归 DNS，Spamhaus 返回错误码。让用户改用本机或 VPC 解析器，或更换 `reputation.dnsbl_zones`。
- 全部候选都是 `reverse_unreachable`：探测目标不可达或 ICMP 被限速，建议更换 `backends.reverse.targets`。
- Globalping 报 429：匿名额度约 250 tests/h，建议填 `backends.globalping.api_token` 或减小 `limit_per_location`。

## 安全边界（不要越过）

- 不要手工 `terminate` 或 `stop` 带 `crossborder-winner=true` 标签的实例，除非用户明确要求并确认。
- `cleanup --include-infra` 会删除共享的安全组与 instance profile，只在用户确认没有 winner 依赖时使用。
- 不要向候选机注入 AWS 凭证；探测脚本只做 ping、tcping。
- 不要把 `config.yaml` 里的 `api_key`、`api_token`、`abuseipdb_api_key` 写进对话或报告。

## 相关文件

- `README.md` 背景与原理；`MANUAL.md` 操作手册；`TESTING.md` 测试方法。
- `ui/report-viewer.html` 报告查看页面：本地打开，载入 `out/<run-id>/report.json` 或 `candidates.csv`。
