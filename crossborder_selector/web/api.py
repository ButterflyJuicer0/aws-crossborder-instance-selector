"""Web 接口的业务实现。只依赖可注入的 boto3 factory，便于测试。"""
import os
from dataclasses import asdict

from botocore.exceptions import BotoCoreError, ClientError

from crossborder_selector.aws.ec2 import Ec2Manager, WINNER_TAG
from crossborder_selector.cli import default_factory, plan_summary
from crossborder_selector.config import load_config, KNOWN_BACKENDS
from crossborder_selector.web import pricing

_SECRET_KEYS = {"api_key", "api_token", "abuseipdb_api_key"}
_BACKEND_DOC = {
    "reverse": {"needs_key": False, "desc": "候选机经 SSM 向大陆三网目标 ping + tcping"},
    "globalping": {"needs_key": False, "desc": "HK/TW 公共探针探测候选 IP"},
    "ripeatlas": {"needs_key": True, "desc": "RIPE Atlas 大陆在线探针，需要 api_key"},
    "itdog": {"needs_key": False, "desc": "itdog.cn 三网家宽节点，非官方接口"},
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def redact(obj):
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _client_factory_with_quotas(factory):
    """CLI 的 factory 只给 ec2/iam/ssm；这里补上 sts 与 service-quotas。"""
    def inner(cfg):
        clients = dict(factory(cfg))
        if "sts" not in clients or "service-quotas" not in clients:
            import boto3
            clients.setdefault("sts", boto3.client("sts", region_name=cfg.region))
            clients.setdefault("service-quotas", boto3.client("service-quotas", region_name=cfg.region))
        return clients
    return inner


class Api:
    def __init__(self, factory=default_factory, config_path=None, cwd=None):
        self.factory = _client_factory_with_quotas(factory)
        self.config_path, self.cwd = config_path, cwd or os.getcwd()

    # ---------- 配置 ----------
    def _config_file(self):
        if self.config_path:
            return self.config_path
        p = os.path.join(self.cwd, "config.yaml")
        return p if os.path.exists(p) else None

    def load(self, overrides: dict):
        try:
            return load_config(self._config_file(), dict(overrides or {}))
        except (ValueError, TypeError) as e:
            raise ApiError(400, f"配置无效：{e}")

    # ---------- ① 环境检查 ----------
    def env(self, region: str) -> dict:
        cfg = self.load({"region": region})
        clients = self.factory(cfg)
        out = {"region": region, "caller": None, "default_vpc": {"present": False, "subnets": 0}, "vcpu_quota": None,
               "running_instances": None, "winners": [], "config_yaml_present": self._config_file() is not None,
               "problems": []}
        try:
            ident = clients["sts"].get_caller_identity()
            out["caller"] = {"account": ident["Account"], "arn": ident["Arn"]}
        except (ClientError, BotoCoreError, KeyError) as e:
            out["problems"].append(f"AWS 凭证不可用：{e}。请先执行 aws configure 或设置环境变量。")
            out["ok"] = False
            return out
        ec2 = clients["ec2"]
        try:
            vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
            if vpcs:
                subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]}])["Subnets"]
                out["default_vpc"] = {"present": True, "subnets": len(subnets)}
            elif not (cfg.subnet_id and cfg.security_group_id):
                out["problems"].append(f"Region {region} 没有默认 VPC。请在 config.yaml 填写 subnet_id 与 security_group_id。")
        except (ClientError, BotoCoreError) as e:
            out["problems"].append(f"无法查询 VPC：{e}")
        try:
            out["vcpu_quota"] = float(clients["service-quotas"].get_service_quota(
                ServiceCode="ec2", QuotaCode="L-1216C47A")["Quota"]["Value"])
        except (ClientError, BotoCoreError, KeyError):
            out["vcpu_quota"] = None
        try:
            r = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}])
            out["running_instances"] = sum(len(res["Instances"]) for res in r["Reservations"])
        except (ClientError, BotoCoreError):
            out["running_instances"] = None
        try:
            r = ec2.describe_instances(Filters=[{"Name": f"tag:{WINNER_TAG}", "Values": ["true"]},
                                                {"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]}])
            for res in r["Reservations"]:
                for i in res["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    out["winners"].append({"instance_id": i["InstanceId"], "public_ip": i.get("PublicIpAddress", ""),
                                           "instance_type": i.get("InstanceType", ""), "score": tags.get("crossborder-score", "")})
        except (ClientError, BotoCoreError):
            pass
        out["ok"] = not out["problems"]
        return out

    # ---------- ② 可选项 ----------
    def options(self, region: str) -> dict:
        cfg = self.load({"region": region})
        clients = self.factory(cfg)
        source, allowed = "catalog", None
        try:
            r = clients["ec2"].describe_instance_type_offerings(
                LocationType="region", Filters=[{"Name": "instance-type", "Values": [i["type"] for i in pricing.INSTANCE_CATALOG]}])
            allowed = {o["InstanceType"] for o in r["InstanceTypeOfferings"]}
            source = "offerings"
        except (ClientError, BotoCoreError, KeyError):
            allowed = None
        types = [i for i in pricing.INSTANCE_CATALOG if allowed is None or i["type"] in allowed]
        backends = []
        for name in KNOWN_BACKENDS:
            b = cfg.backends[name]
            key_present = bool((b.get("api_key") or "").strip()) if _BACKEND_DOC[name]["needs_key"] else True
            backends.append({"name": name, "enabled": b["enabled"], "needs_key": _BACKEND_DOC[name]["needs_key"],
                             "key_present": key_present, "desc": _BACKEND_DOC[name]["desc"]})
        return {"regions": pricing.REGIONS, "instance_types": types, "instance_types_source": source,
                "backends": backends, "defaults": redact(asdict(cfg)), "ipv4_hourly_usd": pricing.IPV4_HOURLY_USD}

    # ---------- ③ 计划 ----------
    def plan(self, overrides: dict) -> dict:
        cfg = self.load(overrides)
        est = pricing.estimate(cfg.instance_type, cfg.batch_size, cfg.max_rounds)
        return {"plan_summary": plan_summary(cfg, "<run-id 将在开始时生成>"), **est, "config": redact(asdict(cfg))}

    # ---------- ⑤/⑥ 选定与清理 ----------
    def select(self, run_id: str, instance_id: str, protect: bool, terminate_others: bool, region: str,
               winner_ids: list) -> dict:
        if instance_id not in winner_ids:
            raise ApiError(400, "该实例不是本次 run 保留的候选，不能选定。")
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        if protect:
            m.protect(instance_id)
        terminated = self._terminate_others(m, instance_id, winner_ids) if terminate_others else []
        return {"selected": instance_id, "protected": bool(protect), "terminated": terminated}

    def terminate_others(self, selected: str, region: str, winner_ids: list) -> dict:
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        return {"terminated": self._terminate_others(m, selected, winner_ids)}

    @staticmethod
    def _terminate_others(m: Ec2Manager, selected: str, winner_ids: list) -> list:
        """终止 selected 之外的其余保留候选：先逐台解除保护再终止；终止失败抛 409。"""
        others = [w for w in winner_ids if w != selected]
        if not others:
            return []
        for iid in others:
            m.unprotect(iid)
        try:
            m.terminate(others)
        except ClientError as e:
            raise ApiError(409, f"无法终止其余候选：{','.join(others)}。{e}。请手工处理或使用 cleanup。")
        return others

    def cleanup(self, run_id: str, region: str) -> dict:
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        ids = m.list_run_instances(run_id)
        m.terminate(ids)
        return {"run_id": run_id, "terminated": ids}
