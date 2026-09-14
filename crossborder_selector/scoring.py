"""子分 → ISP 分 → backend 分 → composite；硬否决；排序。"""
from statistics import mean

from crossborder_selector.config import ISPS
from crossborder_selector.models import CandidateScore


def latency_factor(rtt, good, bad) -> float:
    if rtt is None or rtt >= bad:
        return 0.0
    if rtt <= good:
        return 1.0
    return (bad - rtt) / (bad - good)


def jitter_factor(jitter_ms, jitter_bad_ms, jitter_penalty) -> float:
    """抖动惩罚：抖动达到 jitter_bad_ms 时扣满 jitter_penalty，之间线性；无数据不扣。"""
    if jitter_ms is None or not jitter_bad_ms or not jitter_penalty:
        return 1.0
    return 1.0 - jitter_penalty * min(max(jitter_ms, 0.0) / jitter_bad_ms, 1.0)


def probe_score(p, good, bad, jitter_bad_ms=None, jitter_penalty=0.0) -> float:
    """有 P95 用 P95，否则退回平均值；跨境体验由尾部时延决定，均值会掩盖抖动。"""
    rtt = p.p95_rtt_ms if getattr(p, "p95_rtt_ms", None) is not None else p.median_rtt_ms
    return (100.0 * (1.0 - p.loss) * latency_factor(rtt, good, bad)
            * jitter_factor(getattr(p, "jitter_ms", None), jitter_bad_ms, jitter_penalty))


def isp_scores_for(pr, good, bad, jitter_bad_ms=None, jitter_penalty=0.0) -> dict:
    buckets = {}
    for p in pr.probes:
        buckets.setdefault(p.isp, []).append(probe_score(p, good, bad, jitter_bad_ms, jitter_penalty))
    return {isp: mean(v) for isp, v in buckets.items()}


def backend_score(isp_scores: dict, isp_weights: dict) -> float:
    if not isp_scores:
        return 0.0
    if set(isp_scores) <= set(ISPS):
        total = sum(isp_weights[i] for i in isp_scores)
        return sum(isp_scores[i] * isp_weights[i] for i in isp_scores) / total if total else 0.0
    return mean(isp_scores.values())


def score_candidate(candidate, reputation, probe_results, weights, min_backends, reverse_enabled,
                    prefix_history=None, agent_enabled=False) -> CandidateScore:
    good, bad = weights["lat_good_ms"], weights["lat_bad_ms"]
    jb, jp = weights.get("jitter_bad_ms"), weights.get("jitter_penalty", 0.0)
    valid = [pr for pr in probe_results if pr.ok]
    per_backend_isp = {pr.backend: isp_scores_for(pr, good, bad, jb, jp) for pr in valid}
    backend_scores = {b: backend_score(s, weights["isps"]) for b, s in per_backend_isp.items()}
    bw = {b: weights["backends"].get(b, 0.0) for b in backend_scores}
    total_w = sum(bw.values())
    composite = sum(backend_scores[b] * bw[b] for b in backend_scores) / total_w if total_w else 0.0

    isp_scores = {}
    for isp in set(i for s in per_backend_isp.values() for i in s):
        pairs = [(per_backend_isp[b][isp], bw[b]) for b in per_backend_isp if isp in per_backend_isp[b]]
        tw = sum(w for _, w in pairs)
        isp_scores[isp] = sum(v * w for v, w in pairs) / tw if tw else mean(v for v, _ in pairs)

    veto = ""
    if reputation is not None and reputation.any_listed:
        veto = "reputation"
    elif reverse_enabled:
        rev = next((pr for pr in probe_results if pr.backend == "reverse"), None)
        if rev is None or not rev.ok:
            veto = "reverse_unavailable"
        elif not set(ISPS) <= {p.isp for p in rev.probes}:
            veto = "reverse_incomplete"
        elif all(p.received == 0 for p in rev.probes):
            veto = "reverse_unreachable"
    if not veto and agent_enabled and not any(pr.backend == "agent" and pr.ok for pr in probe_results):
        veto = "agent_unavailable"  # 主信号（China → AWS）缺失时不能凭其他探测源保留
    if not veto and len(valid) < min_backends:
        veto = "min_backends"
    if not veto and not any(p.received > 0 for pr in valid for p in pr.probes):
        veto = "unreachable"

    # prefix 历史融合：同网段过去表现进入最终分，降低单次测量噪声
    instant = round(composite, 2)
    final, hist_score = instant, None
    w_hist = weights.get("prefix_history", 0.0) or 0.0
    min_samples = weights.get("prefix_min_samples", 1) or 1
    entry = (prefix_history or {}).get(candidate.prefix) if candidate.prefix else None
    if w_hist > 0 and entry and entry.get("samples", 0) >= min_samples and entry.get("mean_composite") is not None:
        hist_score = float(entry["mean_composite"])
        final = round((1.0 - w_hist) * instant + w_hist * hist_score, 2)

    return CandidateScore(candidate=candidate, reputation=reputation, probe_results=probe_results,
                          isp_scores=isp_scores, backend_scores=backend_scores,
                          composite=final, qualified=not veto, veto_reason=veto,
                          instant_composite=instant, prefix_history_score=hist_score)


def rank(scores) -> list:
    return sorted(scores, key=lambda s: (s.qualified, s.composite, s.backend_scores.get("reverse", 0.0)),
                  reverse=True)
