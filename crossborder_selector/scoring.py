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


def probe_score(p, good, bad) -> float:
    return 100.0 * (1.0 - p.loss) * latency_factor(p.median_rtt_ms, good, bad)


def isp_scores_for(pr, good, bad) -> dict:
    buckets = {}
    for p in pr.probes:
        buckets.setdefault(p.isp, []).append(probe_score(p, good, bad))
    return {isp: mean(v) for isp, v in buckets.items()}


def backend_score(isp_scores: dict, isp_weights: dict) -> float:
    if not isp_scores:
        return 0.0
    if set(isp_scores) <= set(ISPS):
        total = sum(isp_weights[i] for i in isp_scores)
        return sum(isp_scores[i] * isp_weights[i] for i in isp_scores) / total if total else 0.0
    return mean(isp_scores.values())


def score_candidate(candidate, reputation, probe_results, weights, min_backends, reverse_enabled) -> CandidateScore:
    good, bad = weights["lat_good_ms"], weights["lat_bad_ms"]
    valid = [pr for pr in probe_results if pr.ok]
    per_backend_isp = {pr.backend: isp_scores_for(pr, good, bad) for pr in valid}
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
    if not veto and len(valid) < min_backends:
        veto = "min_backends"
    if not veto and not any(p.received > 0 for pr in valid for p in pr.probes):
        veto = "unreachable"

    return CandidateScore(candidate=candidate, reputation=reputation, probe_results=probe_results,
                          isp_scores=isp_scores, backend_scores=backend_scores,
                          composite=round(composite, 2), qualified=not veto, veto_reason=veto)


def rank(scores) -> list:
    return sorted(scores, key=lambda s: (s.qualified, s.composite, s.backend_scores.get("reverse", 0.0)),
                  reverse=True)
