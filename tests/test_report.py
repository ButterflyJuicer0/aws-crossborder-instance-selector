import csv
import json
from crossborder_selector.config import load_config
from crossborder_selector.models import (Candidate, CandidateScore, IspProbe, ProbeResult,
                                         ReputationResult, RoundResult, RunResult, SourceResult)
from crossborder_selector.report import to_dict, render_markdown, write_reports, update_history, regenerate, CSV_COLUMNS


def _score(iid, ip, prefix, comp, qualified=True, veto=""):
    rep = ReputationResult(ip, [SourceResult("dnsbl", False)], 100.0)
    prs = [ProbeResult("reverse", [IspProbe("telecom", 4, 4, 50.0), IspProbe("unicom", 4, 4, 60.0), IspProbe("mobile", 4, 3, 90.0)]),
           ProbeResult("globalping", [IspProbe("HK", 4, 4, 20.0)]), ProbeResult("ripeatlas", [], "no key")]
    return CandidateScore(Candidate(iid, ip, prefix, 1, "2026-09-07T00:00:00Z"), rep, prs,
                          {"telecom": 95.0, "unicom": 90.0, "mobile": 60.0}, {"reverse": 81.7, "globalping": 100.0},
                          comp, qualified, veto)


def _run():
    a, b = _score("i-1", "18.162.1.1", "18.162.0.0/16", 88.5), _score("i-2", "43.198.2.2", "43.198.0.0/15", 70.0)
    v = CandidateScore(Candidate("i-3", "18.163.3.3", "18.163.0.0/16", 1, ""), ReputationResult("18.163.3.3", [SourceResult("dnsbl", True)], 50.0), [], {}, {}, 0.0, False, "reputation")
    rnd = RoundResult(1, [a.candidate, b.candidate, v.candidate], [v], [a, b], [a], ["i-2", "i-3"], {"ripeatlas": "no key"})
    return RunResult("xb-1", "ap-east-1", [rnd], [a], "2026-09-07T00:00:00Z", "2026-09-07T00:10:00Z", "max_rounds")


def test_to_dict_shape():
    d = to_dict(_run(), load_config(None))
    assert d["run_id"] == "xb-1" and d["region"] == "ap-east-1" and d["stop_reason"] == "max_rounds"
    assert d["winners"][0]["public_ip"] == "18.162.1.1"
    rows = d["candidates"]
    assert len(rows) == 3 and {r["instance_id"] for r in rows} == {"i-1", "i-2", "i-3"}
    r1 = next(r for r in rows if r["instance_id"] == "i-1")
    assert r1["kept"] is True and r1["reverse_telecom"] == 50.0 and r1["globalping_HK"] == 20.0 and r1["ripeatlas_telecom"] is None
    assert d["prefixes"]["18.162.0.0/16"]["samples"] == 1 and d["prefixes"]["18.162.0.0/16"]["best_composite"] == 88.5
    assert d["config"]["batch_size"] == 10 and "abuseipdb_api_key" not in json.dumps(d["config"])
    # globalping api_token 也必须被脱敏
    assert "api_token" not in json.dumps(d["config"])


def test_markdown_mentions_winner_and_warning():
    md = render_markdown(to_dict(_run(), load_config(None)))
    assert "18.162.1.1" in md and "i-1" in md and "stop" in md.lower()
    assert "reputation" in md


def test_write_reports_and_regenerate(tmp_path):
    cfg = load_config(None, {"output_dir": str(tmp_path / "out"), "history_file": str(tmp_path / "h.json")})
    paths = write_reports(_run(), cfg)
    assert all((tmp_path / "out" / "xb-1" / n).exists() for n in ("report.json", "report.md", "candidates.csv"))
    with open(paths["csv"]) as f:
        rows = list(csv.DictReader(f))
    assert [c for c in rows[0]] == CSV_COLUMNS and len(rows) == 3
    hist = json.load(open(paths["history"]))
    assert hist["18.162.0.0/16"]["samples"] == 1
    write_reports(_run(), cfg)  # 第二次 run 合并历史
    assert json.load(open(paths["history"]))["18.162.0.0/16"]["samples"] == 2
    (tmp_path / "out" / "xb-1" / "report.md").unlink()
    regenerate(paths["json"])
    assert (tmp_path / "out" / "xb-1" / "report.md").exists()


def test_update_history_merges_best_and_mean(tmp_path):
    p = str(tmp_path / "h.json")
    update_history(p, {"prefixes": {"a/24": {"samples": 2, "mean_composite": 80.0, "best_composite": 90.0}}, "finished_at": "t1"})
    h = update_history(p, {"prefixes": {"a/24": {"samples": 2, "mean_composite": 60.0, "best_composite": 70.0}}, "finished_at": "t2"})
    assert h["a/24"] == {"samples": 4, "mean_composite": 70.0, "best_composite": 90.0, "last_seen": "t2"}


# ---- prefix 历史读取、P95/抖动字段、agent 列 ----

def test_load_history_missing_file_is_empty(tmp_path):
    from crossborder_selector.report import load_history
    assert load_history(str(tmp_path / "nope.json")) == {}
    (tmp_path / "h.json").write_text(json.dumps({"18.162.0.0/16": {"samples": 3, "mean_composite": 80.0, "best_composite": 90.0}}))
    assert load_history(str(tmp_path / "h.json"))["18.162.0.0/16"]["samples"] == 3


def test_probe_rows_include_p95_jitter_and_history_fields():
    s = _score("i-1", "18.162.1.1", "18.162.0.0/16", 88.5)
    s.probe_results.append(ProbeResult("agent", [IspProbe("telecom", 10, 10, 80.0, p95_rtt_ms=95.0, jitter_ms=3.5,
                                                          target="18.162.1.1:443", method="tcp")]))
    s.instant_composite, s.prefix_history_score = 90.0, 85.0
    rnd = RoundResult(1, [s.candidate], [], [s], [s], [], {})
    run = RunResult("xb-2", "ap-east-1", [rnd], [s], "2026-09-07T00:00:00Z", "2026-09-07T00:10:00Z", "max_rounds")
    row = to_dict(run, load_config(None))["candidates"][0]
    agent_pr = next(pr for pr in row["probe_results"] if pr["backend"] == "agent")
    assert agent_pr["probes"][0]["p95_rtt_ms"] == 95.0 and agent_pr["probes"][0]["jitter_ms"] == 3.5
    assert row["instant_composite"] == 90.0 and row["prefix_history_score"] == 85.0
    assert row["agent_telecom"] == 80.0
    assert "agent_telecom" in CSV_COLUMNS and "instant_composite" in CSV_COLUMNS
