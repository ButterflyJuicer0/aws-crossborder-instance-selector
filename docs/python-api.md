# Python 使用方法

从仓库根目录运行命令。客户端 [examples/api_client.py](../examples/api_client.py) 仅依赖 Python 3 标准库，调用本机 Web API；AWS 凭证由服务进程读取，不需要传给客户端。

## 启动服务与查询

先启动本地服务；已经运行时可直接使用：

```bash
scripts/start_web.sh --no-browser
```

```bash
python3 examples/api_client.py regions
python3 examples/api_client.py types --region ap-east-1
python3 examples/api_client.py images --region ap-east-1 --instance-type t4g.nano
```

`images` 返回当前区域、机型架构匹配的 Amazon Linux 2023、Ubuntu 24.04 LTS、Ubuntu 22.04 LTS，以及实际 AMI ID 和系统盘最小容量。未发布或当前无权查询的镜像不进入选项；查询错误保存在 `error` 中。

`types` 先返回 AWS 提供的全部机型名称，规格字段暂为 `null` 或空值。调用 `images` 后可从 `instance_spec` 读取该机型的 CPU、内存和架构，避免等待全部机型的规格查询。

## 计划与实际启动

[examples/launch.json](../examples/launch.json) 配置两台候选，第二台使用 20 GiB 系统盘。没有指定 AMI ID 时选择 Amazon Linux 2023。

```bash
# 只查看计划；无参数运行脚本也只查看服务默认配置的计划
python3 examples/api_client.py plan --config-json examples/launch.json

# 查询匹配的 Ubuntu 镜像并查看计划
python3 examples/api_client.py plan --config-json examples/launch.json --image ubuntu2404

# 实际创建资源：返回 run_id，后台继续测量
python3 examples/api_client.py start --config-json examples/launch.json --image ubuntu2404 --execute
```

`--region`、`--instance-type`、`--count`、`--rounds`、`--keep`、`--disk-gib`、`--image-id` 可以覆盖 JSON 中对应字段。`--image` 按操作系统查询 AMI，与 `--image-id` 不能同时使用。逐台镜像通过 `instance_overrides` 内的 `image_id` 指定。

多机型使用 `instance_groups`，或重复提供 `--group`：

```bash
python3 examples/api_client.py plan --group t3.nano=2 --group t4g.nano=3 --rounds 1 --image ubuntu2404
python3 examples/api_client.py plan --config-json examples/mixed-launch.json
# 实际启动
python3 examples/api_client.py start --config-json examples/mixed-launch.json --execute
```

清单决定每轮总数，因此不能同时使用 `--instance-type` / `--count` 覆盖它。`image_preset` 会为不同架构选择各自的 AMI；例如默认 `ubuntu2404`，第三台在 `instance_overrides` 中使用 `ubuntu2204`。CLI 的 `--image` 查询清单中所有机型后传递 `image_preset`，启动时由服务解析兼容镜像。逐台配置按清单行顺序展开，最终保留数是全部机型合计。

计划不创建资源，也不证明配额、权限或即时容量满足要求。启动接口在创建前进行检查。实际启动会产生 AWS 费用，保留实例会继续运行。

`--keep`（`keep_top_k`）独立设置最终希望保留的数量，范围 1–50，且不能超过每轮总数乘 `--rounds`。已有候选达到评分目标但保留数量不足时，程序继续下一轮，直到数量满足或用完轮次。合格候选不足时，报告中的保留数量可能少于目标。

## 状态与结果

将 `<run-id>` 替换为启动返回的值：

```bash
python3 examples/api_client.py runs
python3 examples/api_client.py status <run-id>
python3 examples/api_client.py wait <run-id> --timeout 1800
python3 examples/api_client.py report <run-id>
```

等待超时或中断客户端不会取消后台测量。启动请求超时可能已被服务接收，应先查询运行记录，不能直接重复提交。服务重启后的 `unknown` 状态也不能解释为资源已清理。

## 在自己的 Python 脚本中调用

在仓库根目录运行，或把 `api_client.py` 复制到自己的脚本目录后调整 import：

```python
from examples.api_client import SelectorClient

client = SelectorClient()
catalog = client.images("ap-east-1", "t4g.nano")
ubuntu = next(image for image in catalog["images"] if image["key"] == "ubuntu2404")
config = {
    "region": "ap-east-1",
    "instance_type": "t4g.nano",
    "batch_size": 2,
    "max_rounds": 1,
    "keep_top_k": 1,
    "instance_overrides": [
        {},  # 默认 Amazon Linux 2023
        {"image_id": ubuntu["image_id"], "root_volume_size_gib": 20},
    ],
}
print(client.plan(config))

# 需要实际创建时执行以下代码：
# run_id = client.start(config)
# print(run_id)
# status = client.wait(run_id)
# if status["state"] == "finished":
#     print(client.report(run_id))
```

脚本中的 `client.start()` 直接创建资源；命令行的 `--execute` 是示例 CLI 的开关。客户端不自动重试创建请求。

清理未保留候选可使用原 CLI：

```bash
.venv/bin/python -m crossborder_selector.cli cleanup --region ap-east-1 --run-id <run-id>
```

该命令排除带有保留标签的实例。使用运行记录中的实际区域。选定或终止保留实例可在网页结果页操作。

## 模拟验证

```bash
scripts/start_web.sh --demo --port 8766 --output-dir /tmp/crossborder-demo --no-browser
python3 examples/api_client.py --url http://127.0.0.1:8766 start --config-json examples/launch.json --execute
```

此处的服务为模拟模式，不访问 AWS。`--url` 放在子命令之前。
