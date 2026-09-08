# 本地 Web 向导 — 设计文档

日期：2026-09-08
状态：待需求方确认
依赖：`2026-09-07-crossborder-instance-selector-design.md`（核心选机流程，已实现）

## 1. 目标

给交付人员和客户一个能真实运行的本地 Web 界面，按步骤引导完成"检查环境 → 配置参数 → 确认计划 → 运行 → 查看报告 → 选定机器 → 收尾"。界面复用已实现的 `Orchestrator`、`Ec2Manager`、`report` 等模块，不重写选机逻辑。

- 启动方式：`python -m crossborder_selector.web`，默认监听 `127.0.0.1:8765`，浏览器打开即用。
- 使用本机 AWS 凭证；只绑定本机回环地址；不做登录认证。
- 前端为单个 HTML 文件，后端为 Python 标准库 `http.server`，不新增第三方依赖。
- 运行中的日志、每轮结果通过 Server-Sent Events 实时推送。
- 客户可在界面上配置 Region、机型、每轮数量、轮次、保留数、探测 backend、是否加固；运行结束后从保留的候选中选定一台，其余终止。

非目标：多用户、远程部署、HTTPS、权限管理、历史数据分析图表。

## 2. 页面流程（向导六步）

```
① 环境检查          凭证身份、Region、默认 VPC、vCPU 配额、已有 winner
② 配置参数          Region · 机型 · 每轮数量 · 轮次 · 保留数 · backend · protect · 预估成本
③ 确认计划          dry-run 文本 + 成本估算 + 权限提示 → 「开始运行」
④ 运行中            进度条（轮次）、实时日志、每轮候选/否决/保留、已用时、可「取消」
⑤ 结果与选机        保留候选并排（综合分、三网分、prefix、IP）；全部候选表；「选定这台」
⑥ 完成              选定实例信息、接入建议、报告下载（json/md/csv）、「终止其余候选」、清理命令
```

侧栏常驻「历史运行」列表（读取 `out/*/report.json` 与 `out/*/status.json`），点击可回到该 run 的第 ⑤/⑥ 步。

每一步顶部有一句说明这一步在做什么、下一步会发生什么；每个会产生费用或不可逆的操作按钮，点击后先弹确认框并列出影响（启动多少台、终止哪些实例）。

## 3. 后端 API

全部 JSON。错误统一 `{"error": "<中文说明>"}` 与合适的 HTTP 状态码。服务只绑定 `127.0.0.1`。

| 方法与路径 | 作用 |
|---|---|
| `GET /` | 返回 `web/static/index.html` |
| `GET /api/env?region=` | 环境检查：`caller`（`sts get-caller-identity` 的 Account/Arn）、`default_vpc`（有/无，子网数）、`vcpu_quota`（Service Quotas `L-1216C47A`，取不到为 `null`）、`running_instances`（该 Region 运行中实例数）、`winners`（带 `crossborder-winner=true` 的实例列表）、`config_yaml_present` |
| `GET /api/options?region=` | 可选项：`regions`（静态常用列表，默认 `ap-east-1`）、`instance_types`（对该 Region 调 `describe_instance_type_offerings` 过滤一张静态候选表：t3.nano/micro/small、t4g.nano/micro/small、t3.medium、m6g.medium、c6g.medium，各带 vCPU、内存、估算小时价）、`backends`（名称、默认开关、是否需要 key、说明）、`defaults`（来自 `load_config`，含 `config.yaml` 覆盖） |
| `POST /api/plan` | body 为配置覆盖项；返回 `plan_summary` 文本、`estimated_cost_usd`（`batch × rounds × (机型价 + 0.005) × 8/60`）、`estimated_minutes`（`rounds × 8`）、生效后的配置摘要（密钥脱敏） |
| `POST /api/runs` | 用同样的覆盖项启动一次 run（后台线程）；返回 `{"run_id": ...}` |
| `GET /api/runs` | 列出所有 run：`run_id`、`state`（running / finished / failed / cancelled）、`started_at`、`region`、`winners` 摘要 |
| `GET /api/runs/{id}` | 该 run 的状态、最近 200 条事件、结束后的 `report` 摘要（winners、rounds、stop_reason） |
| `GET /api/runs/{id}/events` | SSE 流；先回放已有事件再实时推送；事件类型见 §4 |
| `POST /api/runs/{id}/cancel` | 请求取消：当前轮结束后停止，保留在位 winner，`stop_reason=cancelled` |
| `POST /api/runs/{id}/select` | body `{"instance_id": ..., "protect": bool, "terminate_others": bool}`；对选定实例可选开启 protect，`terminate_others` 为真时终止该 run 其余 winner 实例；返回最终选定信息 |
| `GET /api/runs/{id}/report.json|md|csv` | 直接返回 `out/<run-id>/` 下对应文件 |
| `POST /api/cleanup` | body `{"run_id": ...}`；调用现有 cleanup 逻辑终止该 run 剩余候选机；返回终止的实例 id |

## 4. 运行事件（SSE）

每条事件为一行 `data: {json}`，字段 `type`、`ts`、其余按类型：

| type | 字段 | 触发点 |
|---|---|---|
| `log` | `message` | Orchestrator 的 `log` 回调 |
| `round_started` | `round`, `batch_size` | 每轮 launch 前 |
| `candidates` | `round`, `items:[{instance_id, public_ip, prefix}]` | 拿到公网 IP 后 |
| `vetoed` | `round`, `items:[{instance_id, public_ip, reason}]` | 信誉预筛后 |
| `round_done` | `round`, `kept:[{instance_id, public_ip, composite}]`, `terminated`, `backend_errors` | 每轮结束 |
| `finished` | `stop_reason`, `winners`, `report_paths` | 报告写完 |
| `failed` | `message`, `leftover_instance_ids` | 异常 |

实现方式：`Orchestrator` 新增可选参数 `on_event: Callable[[dict], None]`（默认空函数）和 `should_stop: Callable[[], bool]`（默认恒假）。`_round` 在对应位置调用 `on_event`；`run()` 在每轮开始前检查 `should_stop()`，为真则以 `stop_reason="cancelled"` 结束并照常标记 winner。这是对核心模块唯一的改动，向后兼容，现有测试不变。

## 5. 模块结构

```
crossborder_selector/web/
  __init__.py
  __main__.py        python -m crossborder_selector.web [--port 8765] [--output-dir ./out]
  server.py          ThreadingHTTPServer + 路由分发；只做 HTTP 编解码，不含业务逻辑
  api.py             各接口的业务实现：env / options / plan / start / select / cleanup
  runs.py            RunManager：后台线程、事件队列、状态持久化 out/<run-id>/status.json
  pricing.py         机型候选表与估算小时价（静态，标注为估算值）
  static/index.html  单页前端：向导六步 + 历史运行侧栏
```

- `api.py` 通过与 CLI 相同的 `default_factory(cfg)` 拿 boto3 client，允许测试注入假 factory。
- `RunManager.start(cfg_overrides)`：生成 run-id → 创建 `out/<run-id>/status.json`（state=running）→ 线程内 `ensure_infra` → `Orchestrator(..., on_event=queue.put, should_stop=flag.is_set)` → `write_reports` → 发 `finished`。异常时发 `failed`，并写入 `list_run_instances(run_id)` 供界面提示清理。
- 事件同时写入 `out/<run-id>/events.jsonl`，服务重启后历史运行仍可回看。
- SSE 连接用独立线程从队列读，客户端断开即退出。

## 6. 前端

- 单文件 `index.html`，沿用 `ui/report-viewer.html` 的视觉体系（IBM Plex Sans / Mono，浅深双主题）。
- 状态机：`step` 1～6，`run_id`，`config`；刷新页面后从 `/api/runs` 恢复到运行中的 run。
- 第 ② 步表单：Region 下拉；机型卡片（vCPU、内存、估算价，来自 `/api/options`）；数量/轮次/保留数数字输入；backend 开关（ripeatlas 需 key 时禁用并提示在 `config.yaml` 填写）；protect 复选；右侧实时显示预估成本与时长。
- 第 ④ 步：进度条按 `round/max_rounds`；日志区自动滚动；每轮卡片显示启动/否决/保留；「取消」按钮弹确认。
- 第 ⑤ 步：保留候选以卡片并排，标出综合分与三网分；下方全部候选表可按列排序；「选定这台」弹确认（列出将终止的其余实例）。
- 第 ⑥ 步：显示选定实例、公网 IP、prefix、标签；给出接入建议（不要 stop、只做代理/出口、如何解除 protect）；报告下载按钮；「查看历史运行」。
- 所有金额显示"估算"字样；所有终止操作二次确认。

## 7. 错误处理

| 情况 | 处理 |
|---|---|
| 无 AWS 凭证或 STS 失败 | 第 ① 步显示失败原因与 `aws configure` 提示，禁用「下一步」 |
| 无默认 VPC | 第 ① 步提示在 `config.yaml` 填 `subnet_id`/`security_group_id`；填好后可继续 |
| 配额接口无权限 | `vcpu_quota` 为 `null`，界面显示"未知"，不阻断 |
| run 线程异常 | 状态 `failed`，事件 `failed` 携带遗留实例 id，界面提供「一键清理」调用 `/api/cleanup` |
| 服务进程被关闭 | 运行中的 run 线程随之终止；重启后 `status.json` 仍为 running 的记录标为 `unknown`，界面提示用 `/api/cleanup` 清理 |
| 并发 run | 允许多个 run 并行，各自独立事件队列；界面默认只跟随最新一个 |
| 浏览器关闭 | 不影响后台线程；重开页面从 `/api/runs` 恢复 |

## 8. 测试

- `tests/test_web_api.py`：注入假 boto3 factory，验证 `/api/env`、`/api/options`（offerings 过滤）、`/api/plan` 成本公式、错误 JSON 格式。
- `tests/test_web_runs.py`：用假 Orchestrator 类驱动 `RunManager`，验证事件顺序、`status.json` 状态迁移、cancel 标志、events.jsonl 落盘、`failed` 携带遗留实例。
- `tests/test_web_server.py`：真实启动 `ThreadingHTTPServer` 于随机端口，`urllib` 请求 `/`、`/api/runs`、404、SSE 首包。
- `tests/test_orchestrator.py` 新增：`on_event` 事件序列；`should_stop` 为真时 `stop_reason=cancelled` 且 winner 被标记。
- 前端不做自动化测试；TESTING.md 增加"Web 向导手工冒烟"章节。

## 9. 文档与入口

- README 增加「Web 向导」小节与截图位（文字描述界面）。
- MANUAL 增加"用 Web 向导运行"一节，与 CLI 路径并列。
- `scripts/start_web.sh`：激活 venv 并启动服务，打印访问地址。
- Claude Code skill 增加一条：用户要图形界面时，启动 `python -m crossborder_selector.web` 并给出地址。

## 10. 演示模式

`python -m crossborder_selector.web --demo` 不接触 AWS：`/api/env` 返回固定的成功检查结果，`/api/options` 返回静态机型表，`/api/runs` 用一个模拟 Orchestrator 按真实事件顺序、以加速的时间生成候选、否决、每轮结果与 winner，并写出真实格式的报告文件到 `out/`。用于在没有凭证的环境向客户演示流程；界面顶部显示醒目的"演示模式"标记。
