"""Web 接口的业务实现。只依赖可注入的 boto3 factory，便于测试。"""
import os
import re
import time
import math
from dataclasses import asdict

from botocore.exceptions import BotoCoreError, ClientError

from crossborder_selector.aws.ec2 import Ec2Manager, WINNER_TAG
from crossborder_selector.aws.catalog import collect, region_catalog, instance_catalog, image_catalog, type_spec
from crossborder_selector.aws.infra import prepare_launch, prepare_subnets
from crossborder_selector.cli import default_factory, plan_summary
from crossborder_selector.config import load_config, KNOWN_BACKENDS, launch_groups
from crossborder_selector.web import pricing
from crossborder_selector.diagnostics import redact

_BACKEND_DOC = {
    "agent": {"needs_key": False, "desc": "客户中国区受 SSM 管理的服务器主动探测候选 IP（China → AWS，主信号）；需在 config.yaml 配置 backends.agent.instances"},
    "reverse": {"needs_key": False, "desc": "候选机经 SSM 向大陆三网目标 ping + tcping（AWS → China 回程健康度）"},
    "globalping": {"needs_key": False, "desc": "HK/TW/CN 公共探针探测候选 IP，含逐包 P95 与抖动"},
    "ripeatlas": {"needs_key": True, "desc": "RIPE Atlas 大陆在线探针，需要 api_key"},
    "itdog": {"needs_key": False, "desc": "itdog.cn 中配置的运营商节点，非官方接口"},
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


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
        self._catalog_cache = {}
        self._image_cache = {}

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

    def regions(self):
        try:
            client = self.factory(self.load({}))["ec2"]
            return region_catalog(client)
        except Exception as exc:
            return {**region_catalog(), "error": str(exc)}

    def _types(self, cfg, refresh=False):
        key = (cfg.region, cfg.subnet_id)
        cached = self._catalog_cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        # Offerings return names quickly; reading every model's specifications can
        # take minutes. Fetch only selected models' details via images().
        data = instance_catalog(self.factory(cfg)["ec2"], cfg.subnet_id, include_details=False)
        self._catalog_cache[key] = (time.monotonic(), data)
        return data

    def images(self, region, instance_type):
        cfg = self.load({"region": region, "instance_type": instance_type, "instance_groups": [],
                         "instance_overrides": [], "keep_top_k": 1})
        key = (cfg.region, cfg.instance_type)
        cached = self._image_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        spec = None
        try:
            clients = self.factory(cfg)
            info = clients["ec2"].describe_instance_types(InstanceTypes=[cfg.instance_type])["InstanceTypes"][0]
            spec = type_spec(info)
            data = {**image_catalog(clients["ec2"], clients["ssm"], cfg.instance_type, info=info),
                    "region": cfg.region, "instance_type": cfg.instance_type, "instance_spec": spec, "error": ""}
        except (ClientError, BotoCoreError, KeyError, IndexError) as exc:
            return {"images": [], "region": cfg.region, "instance_type": cfg.instance_type,
                    "instance_spec": spec, "notes": [], "error": f"镜像查询失败：{exc}"}
        self._image_cache[key] = (time.monotonic(), data)
        return data

    @staticmethod
    def _group(instance_type):
        family = re.match(r"[a-z]+", instance_type).group() if instance_type else ""
        if family in set("acdhimrtz"):
            return "standard"
        return "g" if family in ("g", "vt") else family

    def capacity(self, cfg, clients=None):
        clients = clients or self.factory(cfg)
        ec2, quotas = clients["ec2"], clients["service-quotas"]
        groups = launch_groups(cfg)
        names = sorted({g["instance_type"] for g in groups})
        out = {"vcpu_quota": None, "used_vcpus": None, "max_launch_count": None,
               "required_peak_vcpus": None, "groups": [], "problems": [], "notes": []}
        try:
            cpus = {i["InstanceType"]: i["VCpuInfo"]["DefaultVCpus"] for i in collect(
                ec2, "describe_instance_types", "InstanceTypes", InstanceTypes=names)}
            pools = {}
            for g in groups:
                name = g["instance_type"]
                pools.setdefault(self._group(name), []).extend([cpus[name]] * g["count"])
        except (ClientError, BotoCoreError, KeyError, IndexError) as exc:
            out["problems"].append(f"无法确认机型清单：{exc}")
            return out
        usage = None
        try:
            instances = [i for res in collect(ec2, "describe_instances", "Reservations", Filters=[
                {"Name": "instance-state-name", "Values": ["running", "pending", "stopping"]}])
                         for i in res["Instances"] if i.get("InstanceLifecycle") != "spot"]
            if any(not i.get("InstanceType") for i in instances):
                raise ValueError("已有实例缺少机型信息")
            members = [i for i in instances if self._group(i["InstanceType"]) in pools]
            unknown = sorted({i["InstanceType"] for i in members} - cpus.keys())
            for start in range(0, len(unknown), 100):
                for item in collect(ec2, "describe_instance_types", "InstanceTypes", InstanceTypes=unknown[start:start + 100]):
                    cpus[item["InstanceType"]] = item["VCpuInfo"]["DefaultVCpus"]
            usage = {pool: sum(cpus[i["InstanceType"]] for i in members if self._group(i["InstanceType"]) == pool)
                     for pool in pools}
        except (ClientError, BotoCoreError, KeyError, ValueError) as exc:
            out["notes"].append(f"已有用量未知：{exc}")
        other_quotas = None
        for pool, vcpus in pools.items():
            # Any earlier candidates could have survived; reserve the largest possible K.
            previous = sorted(vcpus * (cfg.max_rounds - 1), reverse=True)
            reserved = sum(previous[:cfg.keep_top_k])
            row = {"group": pool, "quota_name": pool, "vcpu_quota": None,
                   "used_vcpus": usage[pool] if usage is not None else None,
                   "requested_vcpus": sum(vcpus), "reserved_vcpus": reserved,
                   "required_peak_vcpus": sum(vcpus) + reserved}
            try:
                quota = None
                if pool == "standard":
                    quota = quotas.get_service_quota(ServiceCode="ec2", QuotaCode="L-1216C47A")["Quota"]
                else:
                    if other_quotas is None:
                        other_quotas = collect(quotas, "list_service_quotas", "Quotas", ServiceCode="ec2")
                    quota = next((q for q in other_quotas if q["QuotaName"].lower().startswith("running on-demand")
                                  and re.search(r"\b" + re.escape(pool) + r"\b", q["QuotaName"].lower())), None)
                if quota:
                    row["vcpu_quota"] = quota["Value"]
                    row["quota_name"] = quota.get("QuotaName", "Standard On-Demand vCPU")
                else:
                    out["notes"].append(f"{pool} 配额未知，启动时由 AWS 判断。")
            except (ClientError, BotoCoreError) as exc:
                out["notes"].append(f"{pool} 配额未知：{exc}")
            limit, used = row["vcpu_quota"], row["used_vcpus"]
            if limit is not None and row["required_peak_vcpus"] + (used or 0) > limit:
                out["problems"].append(f"{pool} vCPU 配额不足：本轮 {sum(vcpus)} + 跨轮保留最多 {reserved}"
                                       f" + 已用 {used if used is not None else '未知'}，配额 {limit}。")
            out["groups"].append(row)
        out["required_peak_vcpus"] = sum(row["required_peak_vcpus"] for row in out["groups"])
        if len(pools) == 1:
            row = out["groups"][0]
            out.update({k: row[k] for k in ("vcpu_quota", "used_vcpus", "quota_name")})
            if len(names) == 1 and row["vcpu_quota"] is not None and row["used_vcpus"] is not None:
                out["max_launch_count"] = max(0, math.floor((row["vcpu_quota"] - row["used_vcpus"] - row["reserved_vcpus"]) / cpus[names[0]]))
        return out

    def env(self, region: str, cfg=None) -> dict:
        check_plan = cfg is not None
        cfg = cfg or self.load({"region": region})
        # 页面要能看到"现在用的是哪个 profile"：即使 STS 失败也先给出 profile 名，便于判断是凭证过期还是选错账户
        profile_env = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE")
        caller = {"profile": profile_env or "default",
                  "profile_source": "AWS_PROFILE" if os.environ.get("AWS_PROFILE") else
                  ("AWS_DEFAULT_PROFILE" if os.environ.get("AWS_DEFAULT_PROFILE") else "默认凭证链"),
                  "account": None, "arn": None}
        out = {"region": region, "caller": caller, "default_vpc": {"present": False, "subnets": 0},
               "config_yaml_present": self._config_file() is not None, "problems": [], "notes": []}
        try:
            clients = self.factory(cfg)
            ident = clients["sts"].get_caller_identity()
            caller.update({"account": ident["Account"], "arn": ident["Arn"]})
        except (ClientError, BotoCoreError) as exc:
            return {**out, "ok": False, "problems": [f"AWS 凭证或区域不可用（profile={caller['profile']}）：{exc}。"
                                                     f"请检查 aws configure、AWS_PROFILE 或重新登录。"]}
        try:
            ec2 = clients["ec2"]
            if cfg.subnet_id:
                subnet = ec2.describe_subnets(SubnetIds=[cfg.subnet_id])["Subnets"][0]
                out["subnet_id"] = subnet["SubnetId"]
            else:
                vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
                if vpcs:
                    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]}])["Subnets"]
                    out["default_vpc"] = {"present": True, "subnets": len(subnets)}
                    if not subnets:
                        out["problems"].append("默认 VPC 没有子网，请填写子网 ID")
                else:
                    out["problems"].append("此区域没有默认 VPC，请填写可访问互联网的子网 ID")
        except (ClientError, BotoCoreError, KeyError, IndexError) as exc:
            out["problems"].append(f"无法确认子网：{exc}")
        if check_plan:
            capacity = self.capacity(cfg, clients)
            out["problems"].extend(capacity.pop("problems"))
            out["notes"].extend(capacity.pop("notes"))
            out.update(capacity)
        out["ok"] = not out["problems"]
        return out

    def preflight(self, cfg):
        status = self.env(cfg.region, cfg)
        if not status["ok"]:
            raise ApiError(400, "；".join(status["problems"]))
        clients = self.factory(cfg)
        try:
            prepare_subnets(clients["ec2"], cfg)
            prepare_launch(clients["ec2"], clients["ssm"], cfg)
        except (ClientError, BotoCoreError, ValueError, RuntimeError) as exc:
            raise ApiError(400, str(exc)) from exc
        return status

    def settings(self, cfg=None):
        cfg = cfg or self.load({})
        backends = []
        for name in KNOWN_BACKENDS:
            b = cfg.backends[name]
            key_present = bool((b.get("api_key") or "").strip()) if _BACKEND_DOC[name]["needs_key"] else True
            backends.append({"name": name, "enabled": b["enabled"], "needs_key": _BACKEND_DOC[name]["needs_key"],
                             "key_present": key_present, "desc": _BACKEND_DOC[name]["desc"]})
        return {"backends": backends, "defaults": redact(asdict(cfg))}

    def agent_status(self, cfg=None) -> dict:
        """agent 面板数据：传输方式、已连接 agent、自动解析出的拨测来源。"""
        from crossborder_selector.cli import probe_source_cidrs, agent_broker, agent_instance_public_ips, agent_timing
        from crossborder_selector.probes import agent_transport
        cfg = cfg or self.load({})
        a = cfg.backends["agent"]
        timing = agent_timing(cfg)
        out = {"enabled": bool(a["enabled"]), "transport": a["transport"], "tcp_ports": list(a["tcp_ports"]),
               "min_agents": a["min_agents"], "probe_source_cidrs": list(a.get("probe_source_cidrs") or []),
               "timeout_s": timing["timeout_s"], "estimated_job_seconds": timing["estimated_job_seconds"],
               "agents": [], "resolved_sources": [], "source_mode": "none", "notes": []}
        if a["enabled"] and timing["too_short"]:
            out["notes"].append(f"backends.agent.timeout_s={timing['timeout_s']} 小于每轮任务预计耗时约 "
                                f"{timing['estimated_job_seconds']}s（{cfg.batch_size} 个目标）：选择器会在 agent 回传前放弃等待，"
                                f"全部候选将按 agent_unavailable 否决。请把 timeout_s 提高到 ≥ {timing['estimated_job_seconds']}，"
                                "或减少每轮台数 / TCP 端口数。")
        registry, instance_ips = {}, None
        if a["transport"] == "http":
            registry = agent_transport.shared_store().registry()
        elif a["transport"] == "s3" and a["enabled"]:
            try:
                registry = agent_broker(a).registry()
            except Exception as exc:  # noqa: BLE001
                out["notes"].append(f"无法读取 S3 心跳：{exc}")
        elif a["transport"] == "ssm" and a["enabled"]:
            instance_ips = agent_instance_public_ips(a)
            registry = {iid: {"isp": isp, "ip": "", "public_ip": "", "last_seen": 0.0} for iid, isp in a["instances"].items()}
        out["agents"] = [{"agent_id": k, "isp": v.get("isp", ""), "ip": v.get("ip", ""), "public_ip": v.get("public_ip", ""),
                          "last_seen": v.get("last_seen", 0.0)} for k, v in sorted(registry.items())]
        if a["enabled"]:
            out["resolved_sources"] = probe_source_cidrs(a, registry=registry, instance_ips=instance_ips)
            out["source_mode"] = "explicit" if out["probe_source_cidrs"] else ("auto" if out["resolved_sources"] else "none")
            if out["source_mode"] == "none":
                out["notes"].append("没有可用的拨测来源：不会开放 TCP 端口，agent 只能测 ICMP。"
                                    "等 agent 连上并自报公网 IP，或在下方手动填写来源 CIDR。")
        return out

    def options(self, region: str, subnet_id=None, refresh=False) -> dict:
        overrides = {"region": region}
        if subnet_id is not None:
            overrides["subnet_id"] = subnet_id
        cfg = self.load(overrides)
        errors = []
        try:
            data = self._types(cfg, refresh=refresh)
        except (ClientError, BotoCoreError, KeyError, IndexError) as exc:
            cached = self._catalog_cache.get((cfg.region, cfg.subnet_id))
            data = cached[1] if cached else {"instance_types": [], "availability_zone": ""}
            errors.append(f"机型查询失败：{exc}")
        source = "aws" if not errors else "aws-cache" if data["instance_types"] else "unavailable"
        return {**data, "instance_types_source": source, "errors": errors,
                **self.settings(cfg)}

    # ---------- ③ 计划 ----------
    def plan(self, overrides: dict) -> dict:
        cfg = self.load(overrides)
        est = pricing.estimate(cfg.instance_type, cfg.batch_size, cfg.max_rounds,
                               region=cfg.region, backends=cfg.backends, keep_top_k=cfg.keep_top_k,
                               ssm_online_timeout_s=cfg.ssm_online_timeout_s, instance_groups=launch_groups(cfg))
        return {"plan_summary": plan_summary(cfg, "<run-id 将在开始时生成>"), **est, "config": redact(asdict(cfg))}

    # ---------- ⑤/⑥ 选定与清理 ----------
    def select(self, run_id: str, instance_id: str, protect: bool, terminate_others: bool, region: str,
               winner_ids: list) -> dict:
        if instance_id not in winner_ids:
            raise ApiError(400, "该实例不是本次 run 保留的候选，不能选定。")
        cfg = self.load({"region": region})
        m = Ec2Manager(self.factory(cfg)["ec2"])
        protection = None
        if protect:
            try:
                protection = m.protect(instance_id)
            except ClientError as exc:
                raise ApiError(409, f"停止或终止保护未全部设置成功，未终止其他实例。请核查 EC2 保护状态后重试：{exc}") from exc
        terminated = self._terminate_others(m, instance_id, winner_ids) if terminate_others else []
        return {"selected": instance_id, "protected": bool(protection), "protection": protection, "terminated": terminated}

    def terminate_others(self, selected: str, region: str, winner_ids: list) -> dict:
        if selected not in winner_ids:
            raise ApiError(400, "无法确认已选定实例，未终止任何候选。")
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
            raise ApiError(409, f"无法终止其余候选：{','.join(others)}。{e}。请核查实例状态后重试；按运行 ID 清理会排除这些已标记为保留的实例。")
        return others

    def cleanup(self, run_id: str, region: str) -> dict:
        cfg = self.load({"region": region})
        ec2 = self.factory(cfg)["ec2"]
        m = Ec2Manager(ec2)
        ids = m.list_run_instances(run_id)
        m.terminate(ids)
        probe_sg_deleted, note = False, ""
        try:
            from crossborder_selector.aws.infra import delete_probe_security_group
            probe_sg_deleted = delete_probe_security_group(ec2, run_id, attempts=1)
        except Exception as e:  # noqa: BLE001 - 实例仍在终止时会 DependencyViolation，稍后再清
            note = f"拨测安全组暂未删除：{e}；实例终止完成后再点一次清理"
        return {"run_id": run_id, "terminated": ids, "probe_sg_deleted": probe_sg_deleted, "note": note}
