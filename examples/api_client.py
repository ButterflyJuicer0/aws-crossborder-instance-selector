"""Python 3 standard-library client for the local selector API.

Run without arguments to preview the server's configured plan.
See docs/python-api.md for query, launch, and per-instance examples.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class SelectorClient:
    def __init__(self, base_url="http://127.0.0.1:8765", timeout=120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path, payload=None):
        request = Request(self.base_url + path,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get("error", str(exc))
            except (ValueError, AttributeError):
                message = str(exc)
            raise RuntimeError(message) from exc

    def regions(self):
        return self._request("/api/regions")

    def types(self, region, subnet_id=""):
        return self._request("/api/options?" + urlencode({"region": region, "subnet_id": subnet_id}))

    def images(self, region, instance_type):
        return self._request("/api/images?" + urlencode({"region": region, "instance_type": instance_type}))

    def plan(self, config):
        return self._request("/api/plan", config)

    def start(self, config):
        """Create resources once. A timeout is ambiguous; inspect runs before retrying."""
        try:
            return self._request("/api/runs", config)["run_id"]
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("启动请求未收到完整响应，结果未知。请先查询 runs 或网页历史，勿直接重复启动。") from exc

    def runs(self):
        return self._request("/api/runs")

    def status(self, run_id):
        return self._request("/api/runs/" + quote(run_id, safe=""))

    def report(self, run_id):
        return self._request("/api/runs/" + quote(run_id, safe="") + "/report.json")

    def wait(self, run_id, timeout=1800, interval=2):
        """Stop polling at the deadline; timeout or Ctrl-C does not cancel the run."""
        if timeout <= 0 or interval <= 0:
            raise ValueError("timeout 和 interval 必须大于 0")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待超时；运行 {run_id} 可能仍在继续，请查询状态。")
            status = self.status(run_id)
            if status["state"] != "running":
                return status
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("regions", help="查询区域")
    commands.add_parser("runs", help="查看运行记录")
    for name in ("types", "images"):
        command = commands.add_parser(name)
        command.add_argument("--region", default="ap-east-1")
        if name == "types":
            command.add_argument("--subnet-id", default="")
        else:
            command.add_argument("--instance-type", default="t3.nano")
    for name in ("plan", "start"):
        command = commands.add_parser(name)
        command.add_argument("--config-json", type=Path)
        command.add_argument("--group", action="append", metavar="TYPE=COUNT", help="机型与台数，可重复；例如 --group t3.nano=2 --group t4g.nano=3")
        for flag, key in (("region", "region"), ("instance-type", "instance_type"), ("image-id", "image_id")):
            command.add_argument("--" + flag, dest=key)
        for flag, key in (("count", "batch_size"), ("rounds", "max_rounds"), ("keep", "keep_top_k"),
                          ("disk-gib", "root_volume_size_gib")):
            command.add_argument("--" + flag, type=int, dest=key)
        command.add_argument("--image", choices=["al2023", "ubuntu2404", "ubuntu2204"],
                             help="按区域和架构查询官方镜像；不能与 --image-id 同用")
        if name == "start":
            command.add_argument("--execute", action="store_true", help="实际提交启动请求")
    for name in ("status", "report", "wait"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "wait":
            command.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    client = SelectorClient(args.url)
    action = args.command or "plan"
    try:
        if action == "regions":
            result = client.regions()
        elif action == "types":
            result = client.types(args.region, args.subnet_id)
        elif action == "images":
            result = client.images(args.region, args.instance_type)
        elif action in ("plan", "start"):
            config_path = getattr(args, "config_json", None)
            config = json.loads(config_path.read_text()) if config_path else {}
            if not isinstance(config, dict):
                raise ValueError("配置文件应为 JSON 对象")
            if getattr(args, "group", None):
                config["instance_groups"] = [{"instance_type": value.rsplit("=", 1)[0], "count": int(value.rsplit("=", 1)[1])}
                                             for value in args.group if "=" in value]
                if len(config["instance_groups"]) != len(args.group):
                    raise ValueError("--group 格式为 TYPE=COUNT")
            if config.get("instance_groups") and (getattr(args, "instance_type", None) or getattr(args, "batch_size", None) is not None):
                raise ValueError("多机型配置请使用 --group 或修改 instance_groups；不能同时指定 --instance-type / --count")
            for key in ("region", "instance_type", "image_id", "batch_size", "max_rounds", "keep_top_k", "root_volume_size_gib"):
                value = getattr(args, key, None)
                if value is not None:
                    config[key] = value
            if getattr(args, "image_id", None):
                config["image_preset"] = ""
            image_key = getattr(args, "image", None)
            if image_key:
                if getattr(args, "image_id", None):
                    raise ValueError("--image 与 --image-id 不能同时使用")
                resolved = client.plan(config)["config"]
                types = {g["instance_type"] for g in resolved.get("instance_groups", [])} or {resolved["instance_type"]}
                for name in sorted(types):
                    catalog = client.images(resolved["region"], name)
                    if not any(i["key"] == image_key for i in catalog["images"]):
                        raise ValueError(catalog.get("error") or f"{name} 在当前区域没有该镜像")
                config["image_id"], config["image_preset"] = "", image_key
            if action == "start" and args.execute:
                result = {"run_id": client.start(config)}
            else:
                result = client.plan(config)
                if action == "start":
                    print("仅显示计划；添加 --execute 才会创建资源。", file=sys.stderr)
        elif action == "runs":
            result = client.runs()
        elif action == "wait":
            result = client.wait(args.run_id, timeout=args.timeout)
        else:
            result = getattr(client, action)(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if action in ("status", "wait") and result.get("state") in ("failed", "unknown") else 0
    except (RuntimeError, ValueError, OSError, URLError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
