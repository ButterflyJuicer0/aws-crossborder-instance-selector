"""命令行入口：select（多轮选机）、cleanup（按 run-id 清理）、report（重生成报告）。"""
import argparse
import ipaddress
import os
import secrets
import sys
from datetime import datetime, timezone
from dataclasses import replace

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import (ensure_infra, delete_infra, delete_shared_iam, security_group_name,
                                            needs_inbound_ping, delete_probe_security_group, probe_sg_name)
from crossborder_selector.aws.ipranges import load_ip_ranges, PrefixLookup
from crossborder_selector.aws.ssm import SsmRunner
from crossborder_selector.config import load_config, launch_groups
from crossborder_selector.orchestrator import Orchestrator
from crossborder_selector.probes.agent import AgentBackend
from crossborder_selector.probes.globalping import GlobalpingBackend
from crossborder_selector.probes.itdog import ItdogBackend
from crossborder_selector.probes.reverse import ReverseBackend
from crossborder_selector.probes.ripeatlas import RipeAtlasBackend
from crossborder_selector.report import write_reports, regenerate, load_history
from crossborder_selector.reputation.abuseipdb import build_sources


def new_run_id() -> str:
    return f"xb-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"


def default_factory(cfg) -> dict:
    return {name: boto3.client(name, region_name=cfg.region) for name in ("ec2", "iam", "ssm", "sts")}


def current_profile() -> str:
    return os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE") or "default"


def verify_credentials(clients):
    """真实运行前用 STS 确认凭证可用。成功返回身份 dict（工厂没提供 sts 客户端时返回 {}，测试替身场景）；
    失败打印可执行的提示并返回 None，由调用方以退出码 2 结束，避免打印出一个空的 run-id 再甩 traceback。"""
    sts = clients.get("sts") if isinstance(clients, dict) else None
    if sts is None:
        return {}
    try:
        ident = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as exc:
        print(f"AWS 凭证不可用（profile={current_profile()}）：{exc}\n"
              f"请 export AWS_PROFILE=<name> 指定有效 profile，或先 aws configure / aws login；"
              f"--dry-run 不需要凭证。", file=sys.stderr)
        return None
    print(f"AWS 身份: {ident.get('Arn')} (账户 {ident.get('Account')}, profile={current_profile()})")
    return ident


def agent_ssm_runner(agent_cfg):
    """agent 实例通常在另一个账户/分区（如中国区），用独立 profile 与区域建 SSM 客户端。"""
    session = boto3.Session(profile_name=agent_cfg["profile"]) if agent_cfg.get("profile") else boto3.Session()
    return SsmRunner(session.client("ssm", region_name=agent_cfg["region"]))


def agent_broker(agent_cfg):
    """http：本进程独立监听器 + 共享信箱；s3：按 profile/region 建 S3 客户端。"""
    if agent_cfg["transport"] == "http":
        from crossborder_selector.probes.agent_transport import get_or_start_listener, shared_store
        get_or_start_listener(agent_cfg["http"]["listen"], agent_cfg["http"].get("token", ""))
        return shared_store()
    from crossborder_selector.probes.agent_transport import S3AgentBroker
    session = boto3.Session(profile_name=agent_cfg["profile"]) if agent_cfg.get("profile") else boto3.Session()
    s3 = session.client("s3", region_name=agent_cfg["region"]) if agent_cfg.get("region") else session.client("s3")
    return S3AgentBroker(s3, agent_cfg["s3"]["bucket"], agent_cfg["s3"].get("prefix", ""))


_NON_ROUTABLE = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")]


def usable_probe_source(value) -> str:
    """能作为安全组放行来源的地址：合法 IP 且不是回环/内网/CGNAT/链路本地/组播。返回规范字符串或空串。
    不用 ipaddress.is_global，它会把 TEST-NET 等文档段也判为非公网。"""
    try:
        addr = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    return "" if any(addr in net for net in _NON_ROUTABLE if net.version == addr.version) else str(addr)


def probe_source_cidrs(agent_cfg, registry=None, instance_ips=None) -> list:
    """决定拨测组允许的 TCP 来源。显式配置 > http 已注册 agent 来源 IP > ssm 实例公网 IP > 空（不开 TCP）。"""
    if not agent_cfg.get("enabled"):
        return []
    explicit = [c for c in agent_cfg.get("probe_source_cidrs") or [] if c]
    if explicit:
        return explicit
    ips = set()
    if agent_cfg.get("transport") in ("http", "s3"):
        for entry in (registry or {}).values():
            # agent 自报的公网出口优先；否则用连接来源。回环/内网/链路本地地址对云上候选无意义，丢弃
            for candidate in (entry.get("public_ip"), entry.get("ip")):
                addr = usable_probe_source(candidate)
                if addr:
                    ips.add(addr)
                    break
    elif agent_cfg.get("transport") == "ssm":
        ips = {ip for ip in (instance_ips or []) if ip}
    return sorted(f"{ip}/{'32' if ':' not in ip else '128'}" for ip in ips)


def agent_instance_public_ips(agent_cfg) -> list:
    """ssm 传输：查 agent 实例的公网 IP，作为拨测来源。查询失败返回空（只测 ICMP）。"""
    try:
        session = boto3.Session(profile_name=agent_cfg["profile"]) if agent_cfg.get("profile") else boto3.Session()
        ec2 = session.client("ec2", region_name=agent_cfg["region"])
        ids = list(agent_cfg.get("instances") or {})
        out = []
        for res in ec2.describe_instances(InstanceIds=ids)["Reservations"]:
            out += [i.get("PublicIpAddress") for i in res["Instances"] if i.get("PublicIpAddress")]
        return out
    except Exception:  # noqa: BLE001
        return []


def resolve_probe_sources(cfg) -> list:
    agent = cfg.backends["agent"]
    if not agent["enabled"]:
        return []
    registry, ips = None, None
    if agent["transport"] == "http":
        from crossborder_selector.probes import agent_transport
        registry = agent_transport.shared_store().registry()
    elif agent["transport"] == "s3":
        try:
            registry = agent_broker(agent).registry()
        except Exception:  # noqa: BLE001 - 读不到心跳就当没有来源
            registry = {}
    elif agent["transport"] == "ssm":
        ips = agent_instance_public_ips(agent)
    return probe_source_cidrs(agent, registry=registry, instance_ips=ips)


def build_backends(cfg, ssm_runner, agent_ssm=None, agent_broker=None) -> list:
    b, out = cfg.backends, []
    if b["agent"]["enabled"]:
        if b["agent"]["transport"] == "ssm":
            out.append(AgentBackend(agent_ssm or agent_ssm_runner(b["agent"]), b["agent"]))
        else:
            from crossborder_selector.probes.agent_transport import RemoteAgentBackend
            out.append(RemoteAgentBackend(agent_broker or globals()["agent_broker"](b["agent"]), b["agent"]))
    if b["reverse"]["enabled"]:
        out.append(ReverseBackend(ssm_runner, b["reverse"]))
    if b["globalping"]["enabled"]:
        out.append(GlobalpingBackend(b["globalping"]))
    if b["ripeatlas"]["enabled"] and (b["ripeatlas"].get("api_key") or "").strip():
        out.append(RipeAtlasBackend(b["ripeatlas"]))
    if b["itdog"]["enabled"]:
        out.append(ItdogBackend(b["itdog"]))
    return out


def plan_summary(cfg, run_id) -> str:
    enabled = [n for n, v in cfg.backends.items() if v["enabled"]
               and not (n == "ripeatlas" and not (v.get("api_key") or "").strip())]
    lines = [f"DRY-RUN run-id={run_id}", f"region={cfg.region}",
             "per round: " + ", ".join(f"{g['count']} x {g['instance_type']}" for g in launch_groups(cfg)) +
             f", max_rounds={cfg.max_rounds}, "
             f"keep_top_k={cfg.keep_top_k}, target_score={cfg.target_score}",
             f"infra: subnet={cfg.subnet_id or '<default VPC>'} sg={cfg.security_group_id or security_group_name(cfg)} "
             f"profile={cfg.instance_profile_name or 'crossborder-selector-ssm'} ami={cfg.image_id or cfg.image_preset or '<AL2023 latest>'}",
             f"root volume: {cfg.root_volume_size_gib or '<AMI default>'} GiB, {cfg.root_volume_type}, encrypted={cfg.root_volume_encrypted}",
             f"per-instance overrides: {cfg.instance_overrides}",
             f"backends: {', '.join(enabled)}", f"protect winner: {cfg.protect}",
             "reverse targets: " + "; ".join(f"{k}={','.join(v)}" for k, v in cfg.backends['reverse']['targets'].items()),
             "inbound: IPv4 ICMP Echo Request from 0.0.0.0/0" if needs_inbound_ping(cfg) else "inbound: no probe ingress required",
             "No AWS resources will be created."]
    return "\n".join(lines)


def _overrides(args) -> dict:
    o = {}
    for k in ("region", "batch_size", "max_rounds", "keep_top_k", "target_score", "instance_type",
              "image_id", "root_volume_size_gib", "root_volume_type", "subnet_id"):
        v = getattr(args, k, None)
        if v is not None:
            o[k] = v
    if getattr(args, "protect", False):
        o["protect"] = True
    if getattr(args, "enable_backend", None):
        o["enable_backends"] = args.enable_backend
    if getattr(args, "disable_backend", None):
        o["disable_backends"] = args.disable_backend
    return o


def _load(args):
    config_path = args.config
    if config_path is None and os.path.exists("config.yaml"):
        config_path = "config.yaml"  # 未显式指定时，自动读取当前目录的 config.yaml
        print("using config.yaml")
    try:
        return load_config(config_path, _overrides(args))
    except ValueError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)


def _do_select(args, factory) -> int:
    cfg, run_id = _load(args), new_run_id()
    if args.dry_run:
        print(plan_summary(cfg, run_id))
        return 0
    clients = factory(cfg)
    if verify_credentials(clients) is None:  # 凭证不可用时在这里退出，不生成空的 run-id
        return 2
    print(f"run-id: {run_id}  (cleanup: python -m crossborder_selector.cli cleanup --region {cfg.region} --run-id {run_id})")
    sources = resolve_probe_sources(cfg)
    if cfg.backends["agent"]["enabled"]:
        print(f"agent 拨测来源: {sources or '无（不开放 TCP，仅 ICMP 可测）'}")
    infra = ensure_infra(clients["ec2"], clients["iam"], clients["ssm"], cfg, run_id=run_id, probe_source_cidrs=sources)
    ssm_runner = SsmRunner(clients["ssm"])
    prefixes = load_ip_ranges(cache_path=os.path.join(cfg.output_dir, "ip-ranges.json"))
    ec2mgr = Ec2Manager(clients["ec2"], log=print)
    orch = Orchestrator(cfg, ec2mgr, ssm_runner, build_backends(cfg, ssm_runner),
                        build_sources(cfg.reputation), PrefixLookup(prefixes, cfg.region), infra, run_id,
                        prefix_history=load_history(cfg.history_file))
    try:
        result = orch.run()
    except KeyboardInterrupt:
        leftovers = ec2mgr.list_run_instances(run_id)
        print(f"interrupted; run cleanup --run-id {run_id} to terminate leftovers: {leftovers}", file=sys.stderr)
        return 130
    except Exception as e:
        surviving = ec2mgr.list_run_instances(run_id)
        print(f"run failed: {e}. surviving run-id-tagged instances: {surviving}; "
              f"clean up with cleanup --run-id {run_id}", file=sys.stderr)
        return 1
    print(f"stop reason: {result.stop_reason}")
    for w in result.winners:
        print(f"WINNER {w.candidate.instance_id} {w.candidate.public_ip} prefix={w.candidate.prefix} score={w.composite}")
    try:  # 报告写入失败不应掩盖 winner 信息
        paths = write_reports(result, cfg)
        print(f"reports: {paths['json']}  {paths['md']}  {paths['csv']}")
    except Exception as e:
        print(f"report failed: {e}", file=sys.stderr)
    print(f"run-id: {run_id}")
    return 0


def _do_cleanup(args, factory) -> int:
    if args.include_iam and not args.include_infra:
        print("--include-iam requires --include-infra", file=sys.stderr)
        return 2
    cfg = _load(args)
    clients = factory(cfg)
    m = Ec2Manager(clients["ec2"], log=print)
    ids = m.list_run_instances(args.run_id)
    m.terminate(ids)
    print(f"terminated {len(ids)} instance(s) tagged crossborder-run-id={args.run_id}: {ids}")
    try:
        if delete_probe_security_group(clients["ec2"], args.run_id):
            print(f"deleted probe security group {probe_sg_name(args.run_id)}")
    except Exception as e:  # noqa: BLE001
        print(f"probe security group not deleted yet: {e}; rerun cleanup after instances finish terminating", file=sys.stderr)
    if args.include_infra:
        if m.has_winners():
            print("retained instances exist in this region; refusing shared infrastructure cleanup", file=sys.stderr)
            return 1
        delete_infra(clients["ec2"], clients["iam"])
        print("deleted managed regional security groups; shared IAM resources retained")
        if args.include_iam:
            delete_shared_iam(clients["ec2"], clients["iam"],
                              lambda region: factory(replace(cfg, region=region))["ec2"])
            print("deleted shared IAM resources after ownership and cross-region dependency checks")
    return 0


def _do_report(args) -> int:
    path = os.path.join(args.output_dir, args.run_id, "report.json")
    paths = regenerate(path)
    print(f"regenerated: {paths['md']}  {paths['csv']}")
    return 0


def _parser():
    p = argparse.ArgumentParser(prog="crossborder-selector")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select", help="多轮测量候选 EC2 并保留得分最高的实例")
    s.add_argument("--config"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--region"); s.add_argument("--instance-type")
    s.add_argument("--image-id"); s.add_argument("--subnet-id")
    s.add_argument("--root-volume-size-gib", type=int)
    s.add_argument("--root-volume-type", choices=("gp3", "gp2", "standard"))
    for k in ("--batch-size", "--max-rounds", "--keep-top-k"):
        s.add_argument(k, type=int)
    s.add_argument("--target-score", type=float)
    s.add_argument("--enable-backend", action="append"); s.add_argument("--disable-backend", action="append")
    s.add_argument("--protect", action="store_true", help="对保留实例开启 API 停止保护和终止保护")
    c = sub.add_parser("cleanup", help="终止指定运行中未标记为保留的候选实例")
    c.add_argument("--config"); c.add_argument("--region"); c.add_argument("--run-id", required=True)
    c.add_argument("--include-infra", action="store_true")
    c.add_argument("--include-iam", action="store_true",
                   help="also check all enabled regions and delete unused managed IAM resources")
    r = sub.add_parser("report", help="从 report.json 重生成 md/csv")
    r.add_argument("--run-id", required=True); r.add_argument("--output-dir", default="./out")
    return p


def main(argv=None, factory=default_factory) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "select":
        return _do_select(args, factory)
    if args.cmd == "cleanup":
        return _do_cleanup(args, factory)
    return _do_report(args)


if __name__ == "__main__":
    sys.exit(main())
