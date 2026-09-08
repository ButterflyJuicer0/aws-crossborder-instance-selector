# 操作手册（MANUAL）

按下列顺序操作即可完成从安装到交付的完整流程。

## 1. 安装

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pytest -q
```

`pytest -q` 应全部通过（当前 128 个）；测试用 moto 模拟 EC2/IAM/SSM，不产生真实资源、不需 AWS 凭证。更完整的测试说明见 [TESTING.md](TESTING.md)。

## 2. 配置

```bash
cp config.example.yaml config.yaml
```

逐字段说明（完整默认值见 `config.example.yaml`）：

- `region`：目标区域，默认 `ap-east-1`（香港）。
- `instance_type`：候选机型，默认 `t3.nano`；arm 机型自动选用 arm64 AMI。
- `batch_size` / `max_rounds` / `keep_top_k`：每轮候选数 / 最大轮次 / 全局保留数。
- `target_score`：综合分达到即提前停止（默认 90）。
- `subnet_id` / `security_group_id`：仅当目标 Region 没有默认 VPC 时必填，否则留空自动准备。
- `backends.*.enabled`：各拨测源开关；`ripeatlas.api_key` 留空则跳过，`itdog` 默认关闭。
- `reputation.abuseipdb_api_key`：留空则跳过 AbuseIPDB，其余信誉源仍生效。

## 3. Dry-run（不创建任何资源）

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run
```

逐行含义：

- `DRY-RUN run-id=...`：本次生成的 run-id，格式 `xb-<UTC 时间>-<4 位十六进制>`。
- `region=`：目标区域。
- `per round:`：每轮启动的实例数与机型、最大轮次、保留数、目标分。
- `infra:`：将复用或创建的子网、安全组 `crossborder-selector-sg`、实例配置 `crossborder-selector-ssm`、AMI。
- `backends:`：本次启用的拨测源。
- `protect winner:`：是否对 winner 开启停止/终止保护（`--protect` 决定）。
- `reverse targets:`：三网回程探测目标。
- `No AWS resources will be created.`：确认 dry-run 不动云资源。

## 4. 冒烟测试

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1
```

预期 5～8 分钟完成。结束后：

- 阅读 `out/<run-id>/report.md`：winners 表给出 instance id、IP、prefix、综合分与三网分；另有每轮淘汰概览与备注。
- 在 EC2 控制台按标签 `crossborder-winner=true` 过滤，确认只剩 1 台候选实例存活，其余已终止。

## 5. 正式运行

```bash
scripts/find_best_instance.sh ap-east-1 20 3 1 --protect
```

`--protect` 会为 winner 开启停止/终止保护。运行中按 Ctrl-C 会中断：进程以退出码 130 结束，并在 stderr 打印残留候选机的 instance id，然后用打印出的 run-id 清理：

```bash
python -m crossborder_selector.cli cleanup --region ap-east-1 --run-id <run-id>
```

## 6. 用 Web 向导运行

与命令行并列的另一条路径。本地 Web 向导按步骤引导完成同一套选机流程，适合交付演示或不熟悉 CLI 参数的场景。

启动：

```bash
scripts/start_web.sh                              # 默认 http://127.0.0.1:8765，自动打开浏览器
scripts/start_web.sh --port 8792 --no-browser     # 换端口、不自动打开浏览器
```

底层等价于 `python -m crossborder_selector.web`，可用参数为 `--host`、`--port`、`--output-dir`、`--config`、`--demo`、`--no-browser`、`--allow-remote`。服务默认只监听 127.0.0.1，使用本机 AWS 凭证，不做登录认证。

`--host` 若指定非回环地址（非 127.0.0.1/localhost/::1），必须同时加 `--allow-remote`，否则直接退出（退出码 2）。这是有意的安全护栏：服务无认证，任何能访问该端口的人都能启动/终止实例，确需远程访问时请自行在网络层限制来源。未加 `--allow-remote` 时，非回环 `Host` 的任意请求、以及跨站 `Origin` 的 POST 都会被拒绝。

六步流程：

1. 环境检查：显示凭证身份、默认 VPC、vCPU 配额与该 Region 已有 winner；存在阻断问题时「下一步」禁用。
2. 配置参数：选机型、每轮候选数、轮次、保留数、backend 与 protect，右侧实时估算成本与时长。
3. 确认计划：一次 dry-run 预览，确认后才启动实例并产生费用。
4. 运行中：进度条、实时日志、每轮候选/否决/保留经 Server-Sent Events 推送。
5. 结果与选机：并排比较保留候选的综合分与三网分，选定一台作为出口机。
6. 完成：显示选定实例、标签、接入建议，并提供 report.json/md/csv 下载。

取消与失败清理：

- 运行中点「取消运行」，当前轮结束后停止，已保留的 winner 留在位，`stop_reason` 记为 `cancelled`。
- run 线程异常时状态转为 `failed`，界面列出遗留实例并提供「立即清理」按钮，等价于对该 run-id 执行 cleanup。

历史运行：右侧常驻侧栏读取 `out/*/status.json` 与报告文件，列出各 run 的状态与 winner，点击可回到该 run 的结果页。服务重启后运行中的线程随旧进程结束，重启前状态仍为 running 的记录标为 `unknown`，界面提示对该 run-id 执行清理以确认没有遗留实例。

演示模式：

```bash
scripts/start_web.sh --demo
```

不接触 AWS。环境检查返回固定的成功结果，运行用模拟数据按真实事件顺序走完六步，并写出真实格式的报告到 `out/`。界面顶部显示醒目的演示模式标记，用于无凭证环境向客户演示。

## 7. 启用可选 backend

- ripeatlas：在 `config.yaml` 的 `backends.ripeatlas` 填 `api_key` 并保证账户有 credits（申请见 https://atlas.ripe.net/docs/getting-started/credits ），填好后自动启用。RIPE Atlas 的 one-off 测量结果通常要数分钟才齐，默认 `timeout_s: 120` 可能只拿到部分探针结果；需要更完整覆盖时调大该值。
- itdog：`scripts/find_best_instance.sh ap-east-1 20 3 1 --enable-backend itdog`。itdog 为非官方 WebSocket 接口，随时可能失效，失败只降级不阻塞本轮。

## 8. 清理

```bash
python -m crossborder_selector.cli cleanup --region ap-east-1 --run-id <run-id>
```

默认只终止带该 run-id 的候选机，保留共享的 SG 与实例配置（winner 依赖）。加 `--include-infra` 会连同删除 `crossborder-selector-sg` 与 `crossborder-selector-ssm`；前提是当前没有任何 `crossborder-winner=true` 实例，否则工具拒绝删除。

## 9. 故障排查

| 现象 | 排查 |
|---|---|
| 候选机 SSM 一直不 online | 确认实例配置 `crossborder-selector-ssm` 已附加、使用 AL2023 AMI、子网可出网（NAT/公网）|
| `InvalidParameterValue`（关联实例配置） | 新建 IAM role 传播延迟，等待后自动重试；持续失败可稍后重跑 |
| RunInstances 配额/容量不足 | 本轮自动缩批并在报告标注；提升该 Region vCPU 配额或减小 `batch_size` |
| ip-ranges 下载失败 | prefix 置空、流程继续；检查网络或稍后重试 |
| 所有候选 `reverse_unreachable` | 探测目标被封或 ICMP 限速，更换 `backends.reverse.targets` 中的三网目标 |
| 每次都 `no_qualified`（信誉全否决） | 使用公共递归 DNS 时 Spamhaus 会对每次查询返回 `127.255.255.254`（经开放递归）或 `127.255.255.255`（被限速），本工具已将其识别为源错误、不计命中；若报告里 dnsbl detail 为 `error:...`，请改用本机/VPC 解析器，或在 `reputation.dnsbl_zones` 换用不受此限的 zone |
| globalping 报错 429 / 频繁失败 | 公共 API 匿名限速约 250 tests/h，批量或多轮容易触顶。在 `backends.globalping.api_token` 填入 token 提升到约 500 tests/h，或减小 `batch_size`/`limit_per_location` |
| RIPE Atlas 结果偏少 | one-off 测量需数分钟才齐，默认 `timeout_s: 120` 内可能只回部分探针；调大 `backends.ripeatlas.timeout_s` |
| `cleanup --include-infra` 报 `DependencyViolation` | SG 仍被运行中的实例（含 winner 或其他 run）占用；先终止相关实例再删，工具会将该错误转为 `RuntimeError` 提示 |
| Web 向导端口被占用（启动报 `Address already in use`）| 用 `scripts/start_web.sh --port <其它端口>` 换端口，例如 `--port 8792` |
| Web 向导页面打不开 | 服务只绑定 127.0.0.1，须在运行服务的本机浏览器访问；不支持从其它机器远程访问 |
| 历史运行里某个 run 显示「状态未知」（unknown）| 服务重启过，该 run 线程已随旧进程结束；对该 run-id 执行 `cleanup --run-id <run-id>` 确认没有遗留候选机 |

## 10. 把 winner 交给生产

- 不要 `stop`：stop/start 会更换公网 IP，reboot 才保留。
- 建议 winner 只跑 Nginx / HAProxy / 代理转发，真实业务放在后端，降低单实例风险。
- 交付前可删除标签或解除保护：
  ```bash
  aws ec2 delete-tags --resources <id> --tags Key=crossborder-winner
  aws ec2 modify-instance-attribute --instance-id <id> --no-disable-api-termination
  aws ec2 modify-instance-attribute --instance-id <id> --no-disable-api-stop
  ```
