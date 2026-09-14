#!/usr/bin/env python3
"""对已有公网 IPv4 复用项目模块做信誉检查与网络探测，不创建任何 AWS 资源。

用法（在仓库根目录，使用项目 .venv）：
  .venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200
  .venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200 --locations HK,TW,CN
  .venv/bin/python .claude/skills/crossborder-select/scripts/probe_ip.py 43.213.150.200 \
      --instance-id i-0123456789abcdef0 --region ap-east-2 --profile personal   # 追加反向探测（经 SSM）

默认执行：DNSBL + GitHub 名单信誉检查（配置了 abuseipdb_api_key 时含 AbuseIPDB），以及 Globalping 探针 ping。
--instance-id 时追加反向探测：该 EC2 经 SSM 向配置中的大陆三网目标 ping/tcping；实例须受 SSM 管理。
RIPE Atlas 在 config.yaml 中 enabled 且填了 api_key 时自动加入；--enable itdog 可加入 itdog。

退出码：0 未命中名单且探测合格；1 命中名单或被否决；2 参数或运行错误。
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from crossborder_selector.config import load_config  # noqa: E402
from crossborder_selector.models import Candidate  # noqa: E402
from crossborder_selector.probes.base import run_backends  # noqa: E402
from crossborder_selector.probes.globalping import GlobalpingBackend  # noqa: E402
from crossborder_selector.reputation.abuseipdb import build_sources  # noqa: E402
from crossborder_selector.reputation.base import score_reputation  # noqa: E402
from crossborder_selector.scoring import score_candidate  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="对已有 IP 做信誉检查与网络探测（不创建 AWS 资源）")
    p.add_argument("ip", help="公网 IPv4")
    p.add_argument("--config", default=None, help="config.yaml 路径；默认读取仓库根目录的 config.yaml（存在时）")
    p.add_argument("--locations", default=None, help="Globalping 探针国家/地区代码，逗号分隔，如 HK,TW,CN；默认取配置")
    p.add_argument("--limit", type=int, default=None, help="每个地区的探针数量；默认取配置")
    p.add_argument("--packets", type=int, default=None, help="每个探针的 ping 包数；默认取配置")
    p.add_argument("--instance-id", default="", help="该 IP 所属 EC2 实例 ID；提供后追加反向探测（经 SSM）")
    p.add_argument("--region", default=None, help="实例所在区域；反向探测必需")
    p.add_argument("--profile", default=None, help="AWS profile；不填走 boto3 默认凭证链")
    p.add_argument("--enable", action="append", default=[], choices=["itdog"], help="额外启用的探测源")
    p.add_argument("--no-globalping", action="store_true", help="跳过 Globalping")
    p.add_argument("--no-reputation", action="store_true", help="跳过信誉检查")
    p.add_argument("--json", action="store_true", help="以 JSON 输出全部结果")
    return p.parse_args(argv)


def build_cfg(args):
    path = args.config
    if path is None:
        default = REPO_ROOT / "config.yaml"
        path = str(default) if default.exists() else None
    overrides = {"region": args.region} if args.region else None
    return load_config(path, overrides)


def build_probe_backends(cfg, args, cand):
    backends, reverse_enabled = [], False
    if not args.no_globalping:
        gp = dict(cfg.backends["globalping"])
        if args.locations:
            gp["locations"] = [s.strip().upper() for s in args.locations.split(",") if s.strip()]
        if args.limit:
            gp["limit_per_location"] = args.limit
        if args.packets:
            gp["packets"] = args.packets
        backends.append(GlobalpingBackend(gp))
    ra = cfg.backends["ripeatlas"]
    if ra.get("enabled") and (ra.get("api_key") or "").strip():
        from crossborder_selector.probes.ripeatlas import RipeAtlasBackend
        backends.append(RipeAtlasBackend(ra))
    if "itdog" in args.enable:
        from crossborder_selector.probes.itdog import ItdogBackend
        backends.append(ItdogBackend(cfg.backends["itdog"]))
    if args.instance_id:
        if not args.region:
            print("error: --instance-id 需要同时提供 --region", file=sys.stderr)
            raise SystemExit(2)
        import boto3
        from crossborder_selector.aws.ssm import SsmRunner
        from crossborder_selector.probes.reverse import ReverseBackend
        session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
        ssm = SsmRunner(session.client("ssm", region_name=args.region))
        online = ssm.wait_online([args.instance_id], min(cfg.ssm_online_timeout_s, 30))
        cand.ssm_online = args.instance_id in online
        backends.append(ReverseBackend(ssm, cfg.backends["reverse"]))
        reverse_enabled = True
    return backends, reverse_enabled


def as_dict(rep, results, errors, score, cand):
    return {
        "ip": cand.public_ip,
        "reputation": None if rep is None else {
            "status": rep.status, "score": rep.score,
            "sources": [{"source": r.source, "status": r.status, "listed": r.listed,
                         "detail": r.detail, "error": r.error} for r in rep.results]},
        "probes": [{"backend": pr.backend, "ok": pr.ok, "error": pr.error,
                    "samples": [{"isp": p.isp, "method": p.method, "target": p.target, "sent": p.sent,
                                 "received": p.received, "loss": round(p.loss, 3), "avg_ms": p.median_rtt_ms}
                                for p in pr.probes]} for pr in results],
        "backend_errors": errors,
        "ssm_online": cand.ssm_online if cand.instance_id != "external" else None,
        "score": None if score is None else {"composite": score.composite, "qualified": score.qualified,
                                             "veto_reason": score.veto_reason,
                                             "backend_scores": score.backend_scores,
                                             "isp_scores": score.isp_scores},
    }


def print_human(d):
    print(f"IP {d['ip']}")
    rep = d["reputation"]
    if rep is not None:
        print(f"\n[信誉] status={rep['status']} score={rep['score']}")
        for s in rep["sources"]:
            extra = f" detail={s['detail']}" if s["detail"] else ""
            extra += f" error={s['error']}" if s["error"] else ""
            print(f"  {s['source']:10} {s['status']:8}{extra}")
    for pr in d["probes"]:
        print(f"\n[{pr['backend']}] ok={pr['ok']}" + (f" error={pr['error']}" if pr["error"] else ""))
        for p in pr["samples"]:
            avg = "-" if p["avg_ms"] is None else f"{p['avg_ms']:.1f}"
            tgt = f" {p['target']}" if p["target"] and p["target"] != d["ip"] else ""
            print(f"  {p['isp']:8} {p['method']:4}{tgt:24} sent={p['sent']} recv={p['received']} "
                  f"loss={p['loss']:.0%} avg_ms={avg}")
    if d["backend_errors"]:
        print("\n[探测源错误]")
        for k, v in d["backend_errors"].items():
            print(f"  {k}: {v}")
    sc = d["score"]
    if sc is not None:
        print(f"\n[评分] composite={sc['composite']} qualified={sc['qualified']} veto={sc['veto_reason'] or '-'}")
        print(f"  backend_scores={sc['backend_scores']}")
        print(f"  isp_scores={sc['isp_scores']}")


def main(argv=None):
    args = parse_args(argv)
    cfg = build_cfg(args)
    cand = Candidate(instance_id=args.instance_id or "external", public_ip=args.ip)

    rep = None
    if not args.no_reputation:
        rcfg = dict(cfg.reputation)
        env_key = os.environ.get("ABUSEIPDB_API_KEY", "").strip()
        if env_key and not (rcfg.get("abuseipdb_api_key") or "").strip():
            rcfg["abuseipdb_api_key"] = env_key
        rep = score_reputation(args.ip, build_sources(rcfg))

    backends, reverse_enabled = build_probe_backends(cfg, args, cand)
    results, errors = ([], {})
    if backends:
        per_ip, errors = run_backends(backends, [cand])
        results = per_ip[args.ip]

    score = None
    if results:
        min_backends = min(cfg.min_backends, len(backends))
        score = score_candidate(cand, rep, results, cfg.weights, min_backends, reverse_enabled)

    out = as_dict(rep, results, errors, score, cand)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print_human(out)

    listed = rep is not None and rep.any_listed
    vetoed = score is not None and not score.qualified
    return 1 if (listed or vetoed) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # 统一转成退出码 2，便于脚本化调用
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
