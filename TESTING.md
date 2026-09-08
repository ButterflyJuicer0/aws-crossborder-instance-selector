# 测试指南（TESTING）

本文给出从纯离线单元测试到真实云冒烟的验证步骤，每步含确切命令与预期输出。

## 1. 单元测试

```bash
. .venv/bin/activate && pytest -q
```

预期结尾输出 `128 passed`。测试不访问网络：EC2/IAM/SSM 用 moto 模拟；SSM 与四个探测 backend（reverse、globalping、ripeatlas、itdog）用注入的假 transport（假 SSM client、假 HTTP、假 WebSocket、假 SSM runner）驱动，配合假 clock 让超时路径可测。

`tests/` 各文件覆盖范围：

- `test_cli.py`：run-id 格式、`build_backends` 遵循 enable 开关、dry-run 不建 client、坏 override 被拒、自动读取当前目录 `config.yaml`、`cleanup` 只终止本 run 并保留 infra、报告写入失败仍打印 winner、`report` 从 json 重生成。
- `test_config.py`：默认值填充、YAML 深合并覆盖默认、CLI override 与 backend 开关、校验规则报错、未知 `--enable-backend` 被拒、已知 backend 常量。
- `test_ec2.py`：launch 打标签与读公网 IP、list/terminate 只影响本 run、`mark_winner` 换标签与 `has_winners`、winner 的 shutdown behavior 设为 `stop`、`--protect` 同时设两个属性、RunInstances 请求整批（MinCount=1）。
- `test_infra.py`：AMI 架构检测、`ensure_infra` 创建后复用、尊重显式传入的 subnet/SG/profile id、缺默认 VPC 报错、`delete_infra` 删受管资源、跳过同名非受管 SG、`DependencyViolation` 转为 `RuntimeError`、arm64 AMI 解析。
- `test_ipranges.py`：下载并返回 prefixes、下载失败返回空、prefix 优先 EC2 再取最具体、缓存写入与复用、lookup 可调用。
- `test_models.py`：`IspProbe.loss`、`ProbeResult.ok`、`ReputationResult.any_listed`、`Candidate` 默认值。
- `test_orchestrator.py`：veto 先终止不进拨测且 winner 保留、锦标赛替换在位 winner 并在 target_score 提前停止、SSM offline 标记与 min_backends veto、`--protect` 与异常时 finally 清理、Ctrl-C 终止全部并传播、后续轮 launch 失败保留在位、首轮 launch 失败重抛、空公网 IP 先被 veto、无幸存者跳过拨测。
- `test_probe_base.py`：`ProbeBackend` 抽象约束、`run_backends` 收集结果并隔离单 backend 异常。
- `test_probe_globalping.py`：请求体与结果解析、超时产出 error、HTTP 错误按候选隔离、无 token 不带 auth header、配置 token 时发 bearer。
- `test_probe_itdog.py`：task token 与参考实现一致、guardret 参考算法、页面解析、全流程节点映射到三网、协议变化降级为 error、recv 超时与 deadline 返回部分样本。
- `test_probe_reverse.py`：`build_script` 覆盖每个目标并带 marker、`parse_output` 解析、无 marker 抛错、`ReverseBackend` 映射 Success/Failed/无 marker 三种状态。
- `test_probe_ripeatlas.py`：ASN → ISP 映射、创建/轮询/映射、超时下部分结果仍有效、无结果记 error。
- `test_report.py`：`to_dict` 结构、markdown 含 winner 与不要 stop 的警告、`write_reports` 与 `regenerate`、prefix 历史合并 best 与 mean。
- `test_reputation.py`：干净 IP 得 100、命中扣权重且下限为 0、源错误不扣分、DNSBL 命中、仅 `127.0.0.0/24` 计命中、error code 不计命中、badlist 命中/未命中/单次抓取、AbuseIPDB 阈值、`build_sources` 按 key 开关。
- `test_scoring.py`：延迟因子边界、子分结合丢包与延迟、多目标取平均、backend 三网加权否则等权、reputation/reverse 不可达/min_backends 三种 veto、composite 在有效 backend 上归一化、排序键。
- `test_ssm.py`：`wait_online` 返回在线集合、超时返回部分、`run_script` 轮询至成功、本地超时路径。
- `test_wrapper_script.py`：包装脚本 dry-run 路径、无参数时打印用法。
- `test_web_api.py`：`/api/env`、`/api/options`（offerings 过滤）、`/api/plan` 成本公式、错误 JSON 格式，全部注入假 boto3 factory。
- `test_web_runs.py`：假 Orchestrator 驱动 `RunManager`，验证事件顺序、`status.json` 状态迁移、cancel 标志、events.jsonl 落盘、`failed` 携带遗留实例。
- `test_web_server.py`：真实启动 `ThreadingHTTPServer` 于随机端口，`urllib` 请求 `/`、`/api/runs`、404、SSE 首包。
- `test_web_pricing.py`：机型候选表与估算小时价。
- `test_web_demo.py`：演示模式的固定环境结果与模拟 run 事件序列。

## 2. Dry-run 验证（不创建资源、不需凭证）

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1 --dry-run
```

预期输出（run-id 每次不同）：

```
DRY-RUN run-id=xb-<UTC 时间>-<4 位十六进制>
region=ap-east-1
per round: 2 x t3.nano, max_rounds=1, keep_top_k=1, target_score=90.0
infra: subnet=<default VPC> sg=crossborder-selector-sg profile=crossborder-selector-ssm ami=<AL2023 latest>
backends: reverse, globalping
protect winner: False
reverse targets: telecom=114.114.114.114:53,www.189.cn; unicom=123.123.123.123:53,www.10010.com; mobile=221.130.33.52:53,www.10086.cn
No AWS resources will be created.
```

各行含义见 [MANUAL.md](MANUAL.md) 第 3 节。末行确认不动云资源；该命令不调用任何 AWS API，无需凭证。

## 3. 真实冒烟（需要 AWS 凭证）

前置检查：

```bash
aws sts get-caller-identity
aws ec2 describe-vpcs --filters Name=isDefault,Values=true --region ap-east-1
aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --region ap-east-1
```

分别确认：凭证可用；目标 Region 有默认 VPC（返回一个 `VpcId`，为空则需在 `config.yaml` 指定 `subnet_id` 与 `security_group_id`）；vCPU 配额（quota code `L-1216C47A` 为 Running On-Demand Standard instances）满足 `batch_size × 2`。

运行冒烟：

```bash
scripts/find_best_instance.sh ap-east-1 2 1 1
```

预计 5～8 分钟。stdout 会打印 run-id、`WINNER <id> <ip> prefix=... score=...`、报告路径与 run-id。

验证结果：

- 确认只剩 1 台带 `crossborder-winner=true` 的实例：
  ```bash
  aws ec2 describe-instances --filters Name=tag:crossborder-winner,Values=true \
    --region ap-east-1 --query 'Reservations[].Instances[].[InstanceId,PublicIpAddress,State.Name]'
  ```
- 检查报告字段：`out/<run-id>/report.md` 的 winners 表含 instance id、IP、prefix、composite、三网分；`out/<run-id>/candidates.csv` 每行一个候选，列包括 `run_id,round,instance_id,public_ip,prefix,reputation_score,veto_reason,composite,qualified,reverse_telecom,reverse_unicom,reverse_mobile,globalping_HK,globalping_TW,ripeatlas_telecom,ripeatlas_unicom,ripeatlas_mobile,itdog_telecom,itdog_unicom,itdog_mobile,kept,terminated`。
- 确认安全组无入站规则：
  ```bash
  aws ec2 describe-security-groups --filters Name=group-name,Values=crossborder-selector-sg \
    --region ap-east-1 --query 'SecurityGroups[0].IpPermissions'
  ```
  预期返回 `[]`。

## 4. 反向探测脚本本地语法检查

无需凭证，只验证生成的 shell 脚本语法正确：

```bash
python -c "from crossborder_selector.probes.reverse import build_script; print(build_script({'telecom':['114.114.114.114:53','www.189.cn']}, 4, 3, 443))" > /tmp/rev.sh && bash -n /tmp/rev.sh && echo OK
```

`bash -n` 只做语法检查、不执行；预期打印 `OK`，无其他报错。脚本里每个目标都会生成一条 `probe_ping` 与一条 `probe_tcp`，末尾以 `CROSSBORDER_JSON:` 前缀输出一行 JSON。

## 5. 清理验证

```bash
python -m crossborder_selector.cli cleanup --region ap-east-1 --run-id <run-id>
```

预期打印 `terminated N instance(s) tagged crossborder-run-id=<run-id>: [...]`。之后确认该 run 的候选机已终止：

```bash
aws ec2 describe-instances --filters Name=tag:crossborder-run-id,Values=<run-id> \
  --region ap-east-1 --query 'Reservations[].Instances[].[InstanceId,State.Name]'
```

winner 已移除 `crossborder-run-id` 标签，故不在此列表内、不受影响。`--include-infra` 会额外删除 `crossborder-selector-sg` 与 `crossborder-selector-ssm`，前提是当前没有任何 `crossborder-winner=true` 实例，否则工具拒绝删除并返回非零退出码。

## 6. 中断恢复验证

在 select 运行过程中按 Ctrl-C。预期进程以退出码 130 结束，并在 stderr 打印形如 `interrupted; run cleanup --run-id <run-id> to terminate leftovers: [...]` 的残留 instance id。随后用该 run-id 清理：

```bash
python -m crossborder_selector.cli cleanup --region ap-east-1 --run-id <run-id>
echo "exit code: $?"
```

用第 5 节的 describe-instances 命令确认残留候选机已终止。

## 7. 单个 backend 的手工验证（可选，需要网络）

在 Python REPL 里对一个已知 IP 调用 GlobalpingBackend：

```python
from crossborder_selector.probes.globalping import GlobalpingBackend
from crossborder_selector.models import Candidate

cfg = {"enabled": True, "locations": ["HK", "TW"], "limit_per_location": 1,
       "packets": 4, "timeout_s": 60, "api_token": ""}
b = GlobalpingBackend(cfg)
res = b.probe([Candidate(instance_id="i-test", public_ip="1.1.1.1")])
print(res["1.1.1.1"])   # ProbeResult(backend='globalping', probes=[IspProbe(isp='HK', ...), ...], error='')
```

返回 `{ip: ProbeResult}`；`error` 非空表示该候选拨测失败。匿名调用受速率限制约 250 tests/h，反复运行易触顶（返回 429），必要时在 cfg 的 `api_token` 填入 token 提升到约 500 tests/h。

## 8. Web 向导冒烟

### 8.1 后端单元测试（不需凭证）

```bash
. .venv/bin/activate && pytest tests/test_web_*.py -q
```

预期 `25 passed`：`test_web_api.py`（8）、`test_web_demo.py`（4）、`test_web_pricing.py`（3）、`test_web_runs.py`（5）、`test_web_server.py`（5）。全部用注入的假 boto3 factory 与假 Orchestrator，不访问网络。

### 8.2 演示模式手工冒烟（不需凭证）

演示模式不接触 AWS，用模拟数据走完整六步，适合无凭证环境验证界面与事件流（对应设计文档 §10）。

```bash
scripts/start_web.sh --demo --no-browser --port 8792
```

启动后打印：

```
Web 向导：http://127.0.0.1:8792（演示模式，不接触 AWS）
```

浏览器打开该地址：顶部显示演示模式横幅；依次走完环境检查 → 配置参数 → 确认计划 → 运行中 → 结果与选机 → 完成。「运行中」页应看到进度条推进、实时日志、每轮候选/否决/保留；「完成」页可下载 report.json/md/csv。

另开一个终端用 curl 验证后端接口：

```bash
curl -s http://127.0.0.1:8792/api/meta
# {"demo": true, "version": "1.0"}

curl -s "http://127.0.0.1:8792/api/env?region=ap-east-1"
# {"region": "ap-east-1", "caller": {"account": "123456789012", ...}, "default_vpc": {"present": true, "subnets": 2}, "vcpu_quota": 64.0, "running_instances": 3, "winners": [], "config_yaml_present": false, "problems": [], "ok": true}

curl -s "http://127.0.0.1:8792/api/options?region=ap-east-1"
# {"regions": [...7 项...], "instance_types": [...9 项，各带 vcpu/memory_gib/arch/hourly_usd...], "backends": [reverse, globalping, ripeatlas, itdog], "defaults": {...}}
```

发起一次 run 并用 SSE 观察事件流：

```bash
RID=$(curl -s -X POST http://127.0.0.1:8792/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"region":"ap-east-1","batch_size":2,"max_rounds":1,"keep_top_k":1}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
curl -N "http://127.0.0.1:8792/api/runs/$RID/events"
```

预期按顺序收到（run-id 每次不同，形如 `xb-20260908T081749Z-375c`）：

```
data: {"type": "round_started", "round": 1, "batch_size": 2, ...}
data: {"type": "log", "message": "[demo round 1] launching 2 candidates", ...}
data: {"type": "candidates", "round": 1, "items": [{"instance_id": "i-demo100", "public_ip": "18.162.253.159", "prefix": "18.162.0.0/16"}, ...]}
data: {"type": "vetoed", "round": 1, "items": [{"instance_id": "i-demo100", "public_ip": "18.162.253.159", "reason": "reputation"}]}
data: {"type": "round_done", "round": 1, "kept": [{"instance_id": "i-demo101", "public_ip": "43.198.168.205", "composite": 87.5}], "terminated": ["i-demo100"], "backend_errors": {}}
data: {"type": "finished", "stop_reason": "max_rounds", "winners": [{"instance_id": "i-demo101", "composite": 87.5, ...}], "report_paths": {...}}
```

收到 `finished` 后流自动关闭；演示报告写在 `out/<run-id>/`。用完停止服务：

```bash
pkill -f crossborder_selector.web
```

### 8.3 真实模式冒烟（需要凭证）

前置检查与第 3 节相同（凭证、默认 VPC、vCPU 配额）。启动向导后走真实运行：

```bash
scripts/start_web.sh --port 8792
```

在界面从「配置参数」按 2 台 1 轮发起，或直接走默认规模。「运行中」页的 SSE 事件与命令行 select 一致，约 5～8 分钟完成；「结果与选机」页选定一台后其余保留候选按勾选终止。用第 5 节的 describe-instances 命令确认只剩选定实例带 `crossborder-winner=true`。

### 8.4 SSE 单独验证

对一个运行中的 run 直接观察事件流（真实或演示均可）：

```bash
curl -N http://127.0.0.1:8765/api/runs/<run-id>/events
```

每行为 `data: {json}`，事件类型依次为 `round_started`、`candidates`、`vetoed`、`round_done`；其间穿插若干 `log` 行（后台进度文本，前端原样打进日志框）。若运行中调用了 `POST /api/runs/<run-id>/cancel`，会收到一条 `cancelled` 事件（提示当前轮结束后停止、保留在位 winner），随后本轮结束时仍以 `finished`（`stop_reason=cancelled`）收尾。run 结束时收到 `finished`（异常时 `failed`），SSE 在回放到终态后即结束。对已结束的 run 再调 cancel 返回 404。

### 8.5 前端渲染检查（可选）

前端不做自动化测试。可用 headless Chrome 截图确认页面能正常渲染：

```bash
scripts/start_web.sh --demo --no-browser &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=/tmp/wizard.png http://127.0.0.1:8765/
```

生成 `/tmp/wizard.png`，打开确认顶部有演示模式横幅、左侧六步导航、主区为环境检查卡片。
