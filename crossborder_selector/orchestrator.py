"""滚动锦标赛：每轮启动候选机 → 信誉预筛 → 拨测 → 与在位者合并保留 Top-K → 终止其余。"""
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from crossborder_selector.models import Candidate, CandidateScore, RoundResult, RunResult
from crossborder_selector.probes.base import run_backends
from crossborder_selector.reputation.base import score_reputation
from crossborder_selector.scoring import score_candidate, rank


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Orchestrator:
    def __init__(self, cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id,
                 clock=utc_now_iso, log=print):
        self.cfg, self.ec2, self.ssm = cfg, ec2, ssm
        self.backends, self.rep_sources = backends, reputation_sources
        self.prefix_lookup, self.infra, self.run_id = prefix_lookup, infra, run_id
        self.now, self.log = clock, log
        self.reverse_enabled = any(b.name == "reverse" for b in backends)

    def run(self) -> RunResult:
        started, rounds, incumbents, stop = self.now(), [], [], "max_rounds"
        launched_all = set()
        try:
            for rno in range(1, self.cfg.max_rounds + 1):
                self.log(f"[round {rno}] launching {self.cfg.batch_size} x {self.cfg.instance_type}")
                try:
                    ids = self.ec2.launch(self.cfg.batch_size, self.run_id, rno, self.infra,
                                          self.cfg.instance_type)
                except ClientError as e:
                    if not incumbents:
                        raise  # 首轮启动失败：无在位者可保底，按原逻辑清理并上抛
                    # 后续轮启动失败：不牵连上一轮在位者，记一条空轮并进入 winner 处理
                    rounds.append(RoundResult(rno, [], [], [], incumbents, [], {"launch": str(e)}))
                    stop = "launch_failed"
                    break
                rr = self._round(rno, ids, incumbents)
                launched_all.update(c.instance_id for c in rr.launched)
                rounds.append(rr)
                incumbents = rr.kept
                if incumbents and incumbents[0].composite >= self.cfg.target_score:
                    stop = "target_score_reached"
                    break
        except BaseException:  # 含 KeyboardInterrupt：终止本 run 已启动的非在位者后再上抛
            keep = {s.candidate.instance_id for s in incumbents}
            self.ec2.terminate(sorted(launched_all - keep))
            raise
        if not incumbents:
            stop = "no_qualified"
        for w in incumbents:
            self.ec2.mark_winner(w.candidate.instance_id, self.run_id, w.composite, w.candidate.round, self.now())
            if self.cfg.protect:
                self.ec2.protect(w.candidate.instance_id)
        return RunResult(self.run_id, self.cfg.region, rounds, incumbents, started, self.now(), stop)

    def _round(self, rno, ids, incumbents) -> RoundResult:
        terminated = []
        try:
            self.ec2.wait_running(ids)
            ips = self.ec2.public_ips(ids)
            cands = [Candidate(i, ips[i], self.prefix_lookup(ips[i]) if ips[i] else "", rno, self.now())
                     for i in ids]

            vetoed, survivors = [], []
            for c in cands:
                rep = score_reputation(c.public_ip, self.rep_sources)
                if rep.any_listed:
                    vetoed.append(CandidateScore(c, rep, [], {}, {}, 0.0, False, "reputation"))
                else:
                    survivors.append((c, rep))
            self.ec2.terminate([v.candidate.instance_id for v in vetoed])
            terminated += [v.candidate.instance_id for v in vetoed]

            online = self.ssm.wait_online([c.instance_id for c, _ in survivors], self.cfg.ssm_online_timeout_s)
            for c, _ in survivors:
                c.ssm_online = c.instance_id in online
            results, errors = run_backends(self.backends, [c for c, _ in survivors])
            scored = [score_candidate(c, rep, results.get(c.public_ip, []), self.cfg.weights,
                                      self.cfg.min_backends, self.reverse_enabled) for c, rep in survivors]

            pool = rank(list(incumbents) + scored)
            kept = [s for s in pool if s.qualified][: self.cfg.keep_top_k]
            keep_ids = {s.candidate.instance_id for s in kept}
            losers = [s.candidate.instance_id for s in pool if s.candidate.instance_id not in keep_ids]
            self.ec2.terminate(losers)
            terminated += losers
            self.log(f"[round {rno}] kept={[s.candidate.public_ip for s in kept]} terminated={len(terminated)}")
            return RoundResult(rno, cands, vetoed, scored, kept, terminated, errors)
        except BaseException:  # 含 KeyboardInterrupt：先终止本轮实例再上抛
            self.ec2.terminate([i for i in ids if i not in terminated])
            raise
