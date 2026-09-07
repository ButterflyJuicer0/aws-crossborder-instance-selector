"""JSON / Markdown / CSV 报告与 prefix 历史。"""
import csv
import json
import os
from dataclasses import asdict
from statistics import mean

from crossborder_selector.config import ISPS

BACKEND_ISP_COLUMNS = [("reverse", i) for i in ISPS] + [("globalping", "HK"), ("globalping", "TW")] + \
                      [("ripeatlas", i) for i in ISPS] + [("itdog", i) for i in ISPS]
CSV_COLUMNS = ["run_id", "round", "instance_id", "public_ip", "prefix", "reputation_score", "veto_reason",
               "composite", "qualified"] + [f"{b}_{i}" for b, i in BACKEND_ISP_COLUMNS] + ["kept", "terminated"]
_SECRET_KEYS = {"api_key", "abuseipdb_api_key"}


def _redact(obj):
    """报告里直接删除密钥字段，不保留键名。"""
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _raw_rtt(score, backend, isp):
    """该 backend 对该 ISP 的原始 rtt 中位数均值（ms）；无数据 None。"""
    for pr in score.probe_results:
        if pr.backend == backend and pr.ok:
            vals = [p.median_rtt_ms for p in pr.probes if p.isp == isp and p.median_rtt_ms is not None]
            return round(mean(vals), 1) if vals else None
    return None


def _row(run, s, kept_ids, terminated_ids):
    c = s.candidate
    row = {"run_id": run.run_id, "round": c.round, "instance_id": c.instance_id, "public_ip": c.public_ip,
           "prefix": c.prefix, "reputation_score": s.reputation.score if s.reputation else None,
           "veto_reason": s.veto_reason, "composite": s.composite, "qualified": s.qualified}
    for b, i in BACKEND_ISP_COLUMNS:
        row[f"{b}_{i}"] = _raw_rtt(s, b, i)
    row["kept"] = c.instance_id in kept_ids
    row["terminated"] = c.instance_id in terminated_ids
    row["isp_scores"], row["backend_scores"] = s.isp_scores, s.backend_scores
    return row


def to_dict(run, cfg) -> dict:
    kept_ids = {w.candidate.instance_id for w in run.winners}
    terminated = {i for r in run.rounds for i in r.terminated}
    scores = [s for r in run.rounds for s in (r.vetoed + r.scored)]
    rows = [_row(run, s, kept_ids, terminated) for s in scores]
    prefixes = {}
    for s in scores:
        if s.candidate.prefix and s.qualified:
            prefixes.setdefault(s.candidate.prefix, []).append(s.composite)
    prefix_stats = {p: {"samples": len(v), "mean_composite": round(mean(v), 2), "best_composite": max(v)}
                    for p, v in prefixes.items()}
    return {
        "run_id": run.run_id, "region": run.region, "started_at": run.started_at, "finished_at": run.finished_at,
        "stop_reason": run.stop_reason, "rounds_completed": len(run.rounds),
        "config": _redact(asdict(cfg)),
        "winners": [_row(run, w, kept_ids, terminated) for w in run.winners],
        "rounds": [{"round": r.round, "launched": len(r.launched), "vetoed": len(r.vetoed),
                    "scored": len(r.scored), "kept": [s.candidate.public_ip for s in r.kept],
                    "terminated": r.terminated, "backend_errors": r.backend_errors} for r in run.rounds],
        "candidates": rows, "prefixes": prefix_stats,
    }


def render_markdown(d: dict) -> str:
    L = [f"# 跨境优选实例报告 {d['run_id']}", "",
         f"- Region：{d['region']}", f"- 时间：{d['started_at']} → {d['finished_at']}",
         f"- 轮数：{d['rounds_completed']}，停止原因：{d['stop_reason']}", "", "## Winners", ""]
    if d["winners"]:
        L += ["| instance | public IP | prefix | composite | 电信 | 联通 | 移动 |", "|---|---|---|---|---|---|---|"]
        for w in d["winners"]:
            s = w["isp_scores"]
            L.append(f"| {w['instance_id']} | {w['public_ip']} | {w['prefix']} | {w['composite']} | "
                     f"{s.get('telecom', '-')} | {s.get('unicom', '-')} | {s.get('mobile', '-')} |")
        L += ["", "> **不要 stop 这些实例。** stop/start 会更换公网 IPv4；reboot 不会。",
              "> 实例已移除 run-id 标签并打上 `crossborder-winner=true`，`cleanup` 不会终止它们。"]
    else:
        L.append("本次没有合格候选。")
    L += ["", "## 每轮概览", "", "| round | launched | vetoed | scored | kept | terminated | backend errors |", "|---|---|---|---|---|---|---|"]
    for r in d["rounds"]:
        L.append(f"| {r['round']} | {r['launched']} | {r['vetoed']} | {r['scored']} | {', '.join(r['kept']) or '-'} | "
                 f"{len(r['terminated'])} | {', '.join(r['backend_errors']) or '-'} |")
    L += ["", "## 全部候选", "", "| round | IP | prefix | composite | qualified | veto |", "|---|---|---|---|---|---|"]
    for c in sorted(d["candidates"], key=lambda x: (-x["composite"], x["round"])):
        L.append(f"| {c['round']} | {c['public_ip']} | {c['prefix']} | {c['composite']} | {c['qualified']} | {c['veto_reason'] or '-'} |")
    L += ["", "## Prefix 统计（合格候选）", "", "| prefix | samples | mean | best |", "|---|---|---|---|"]
    for p, v in sorted(d["prefixes"].items(), key=lambda kv: -kv[1]["best_composite"]):
        L.append(f"| {p} | {v['samples']} | {v['mean_composite']} | {v['best_composite']} |")
    return "\n".join(L) + "\n"


def write_csv(d: dict, path: str):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in d["candidates"]:
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in CSV_COLUMNS})


def update_history(path: str, d: dict) -> dict:
    hist = {}
    if os.path.exists(path):
        with open(path) as f:
            hist = json.load(f)
    for p, v in d.get("prefixes", {}).items():
        h = hist.get(p, {"samples": 0, "mean_composite": 0.0, "best_composite": 0.0, "last_seen": ""})
        n = h["samples"] + v["samples"]
        h["mean_composite"] = round((h["mean_composite"] * h["samples"] + v["mean_composite"] * v["samples"]) / n, 2)
        h["samples"], h["best_composite"] = n, max(h["best_composite"], v["best_composite"])
        h["last_seen"] = d.get("finished_at", "")
        hist[p] = h
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(hist, f, indent=2, ensure_ascii=False)
    return hist


def _write_all(d: dict, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    paths = {"json": os.path.join(out_dir, "report.json"), "md": os.path.join(out_dir, "report.md"),
             "csv": os.path.join(out_dir, "candidates.csv")}
    with open(paths["json"], "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    with open(paths["md"], "w") as f:
        f.write(render_markdown(d))
    write_csv(d, paths["csv"])
    return paths


def write_reports(run, cfg, out_dir=None) -> dict:
    d = to_dict(run, cfg)
    paths = _write_all(d, os.path.join(out_dir or cfg.output_dir, run.run_id))
    update_history(cfg.history_file, d)
    paths["history"] = cfg.history_file
    return paths


def regenerate(report_json_path: str) -> dict:
    with open(report_json_path) as f:
        d = json.load(f)
    return _write_all(d, os.path.dirname(report_json_path))
