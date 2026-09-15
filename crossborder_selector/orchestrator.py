"""滚动锦标赛：每轮启动候选机 → 信誉预筛 → 拨测 → 与在位者合并保留 Top-K → 终止其余。"""
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from crossborder_selector.models import Candidate, CandidateScore, RoundResult, RunResult
from crossborder_selector.probes.base import run_backends
from crossborder_selector.reputation.base import score_reputation
from crossborder_selector.scoring import score_candidate, rank
from crossborder_selector.config import launch_groups, keep_quotas


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Orchestrator:
    def __init__(self, cfg, ec2, ssm, backends, reputation_sources, prefix_lookup, infra, run_id,
                 clock=utc_now_iso, log=print, on_event=None, should_stop=None, prefix_history=None):
        self.cfg, self.ec2, self.ssm = cfg, ec2, ssm
        self.backends, self.rep_sources = backends, reputation_sources
        self.prefix_lookup, self.infra, self.run_id = prefix_lookup, infra, run_id
        self.now, self.log = clock, log
        self.reverse_enabled = any(b.name == "reverse" for b in backends)
        self.agent_enabled = any(b.name == "agent" for b in backends)
        self.prefix_history = dict(prefix_history or {})  # 运行开始时的快照，本次结果不回喂自身
        # Web 层可注入的钩子：on_event 广播每轮进度，should_stop 供用户取消
        self.on_event = on_event or (lambda e: None)
        self.should_stop = should_stop or (lambda: False)

    def _emit(self, type_, **fields):
        self.on_event({"type": type_, "ts": self.now(), **fields})

    def run(self) -> RunResult:
        started, rounds, incumbents, stop = self.now(), [], [], "max_rounds"
        launched_all = set()
        try:
            for rno in range(1, self.cfg.max_rounds + 1):
                if self.should_stop():  # 每轮 launch 前检查取消
                    stop = "cancelled"
                    break
                self._emit("round_started", round=rno, batch_size=self.cfg.batch_size)
                description = ", ".join(f"{g['count']} x {g['instance_type']}" for g in launch_groups(self.cfg))
                self.log(f"[round {rno}] launching {description}")
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
                if len(incumbents) >= self.cfg.keep_top_k and incumbents[0].composite >= self.cfg.target_score:
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
        self._retire_probe_access(incumbents)
        return RunResult(self.run_id, self.cfg.region, rounds, incumbents, started, self.now(), stop)

    def _retire_probe_access(self, incumbents):
        """保留实例回到"只带共享组"的状态：摘掉本次拨测组、停掉临时监听，再删除拨测组。任何一步失败只记日志。"""
        probe_sg = getattr(self.infra, "probe_security_group_id", "")
        if not probe_sg:
            return
        from crossborder_selector.aws.infra import STOP_LISTENER_SCRIPT
        for w in incumbents:
            iid = w.candidate.instance_id
            try:
                self.ec2.detach_probe_group(iid, probe_sg)
            except Exception as e:  # noqa: BLE001 - 收尾动作不应让已完成的运行失败
                self.log(f"[retire] {iid} 摘除拨测组失败：{e}")
            try:
                status, _ = self.ssm.run_script(iid, STOP_LISTENER_SCRIPT, timeout_s=60)
                if status != "Success":
                    self.log(f"[retire] {iid} 停止临时监听返回 {status}")
            except Exception as e:  # noqa: BLE001
                self.log(f"[retire] {iid} 停止临时监听失败：{e}")
        try:
            self.ec2.delete_security_group(probe_sg)
        except Exception as e:  # noqa: BLE001
            self.log(f"[retire] 删除拨测组 {probe_sg} 失败：{e}；可用 cleanup --run-id 重试")

    def _select_kept(self, pool) -> list:
        """从已排序的候选池中选出保留者：按机型配额时每种机型各取合格的前 keep 台，否则全局前 keep_top_k。"""
        quotas = keep_quotas(self.cfg)
        qualified = [s for s in pool if s.qualified]
        if quotas is None:
            return qualified[: self.cfg.keep_top_k]
        taken = {t: 0 for t in quotas}
        kept = []
        for s in qualified:
            t = s.candidate.instance_type
            if taken.get(t, 0) < quotas.get(t, 0):
                kept.append(s)
                taken[t] = taken.get(t, 0) + 1
        return kept

    def _round(self, rno, ids, incumbents) -> RoundResult:
        terminated = []
        try:
            self.ec2.wait_running(ids)
            ips = self.ec2.public_ips(ids)
            cands = [Candidate(i, ips[i], self.prefix_lookup(ips[i]) if ips[i] else "", rno, self.now())
                     for i in ids]
            for candidate in cands:
                template = getattr(self.ec2, "launch_settings", {}).get(candidate.instance_id, {})
                candidate.image_id = template.get("ImageId", "")
                candidate.instance_type = template.get("InstanceType", self.cfg.instance_type)
                candidate.root_volume = next((m["Ebs"] for m in template.get("BlockDeviceMappings", []) if "Ebs" in m), {})
            self._emit("candidates", round=rno,
                       items=[{"instance_id": c.instance_id, "public_ip": c.public_ip, "prefix": c.prefix, "instance_type": c.instance_type}
                              for c in cands])

            vetoed, survivors = [], []
            for c in cands:
                if not c.public_ip:  # 未拿到公网 IP：无法探测，直接否决
                    vetoed.append(CandidateScore(c, None, [], {}, {}, 0.0, False, "no_public_ip"))
                    continue
                rep = score_reputation(c.public_ip, self.rep_sources)
                if rep.any_listed:
                    vetoed.append(CandidateScore(c, rep, [], {}, {}, 0.0, False, "reputation"))
                elif self.cfg.reputation.get("require_badlist", True) and any(
                        r.source == "badlist" and r.status == "unknown" for r in rep.results):
                    vetoed.append(CandidateScore(c, rep, [], {}, {}, 0.0, False, "reputation_unavailable"))
                else:
                    survivors.append((c, rep))
            self.ec2.terminate([v.candidate.instance_id for v in vetoed])
            terminated += [v.candidate.instance_id for v in vetoed]
            self._emit("vetoed", round=rno,
                       items=[{"instance_id": v.candidate.instance_id, "public_ip": v.candidate.public_ip,
                               "reason": v.veto_reason} for v in vetoed])

            if survivors:
                online = self.ssm.wait_online([c.instance_id for c, _ in survivors], self.cfg.ssm_online_timeout_s)
                for c, _ in survivors:
                    c.ssm_online = c.instance_id in online
                results, errors = run_backends(self.backends, [c for c, _ in survivors])
            else:  # 无幸存者：跳过上线等待与拨测
                results, errors = {}, {}
            scored = [score_candidate(c, rep, results.get(c.public_ip, []), self.cfg.weights,
                                      self.cfg.min_backends, self.reverse_enabled,
                                      prefix_history=self.prefix_history, agent_enabled=self.agent_enabled)
                      for c, rep in survivors]

            pool = rank(list(incumbents) + scored)
            kept = self._select_kept(pool)
            keep_ids = {s.candidate.instance_id for s in kept}
            losers = [s.candidate.instance_id for s in pool if s.candidate.instance_id not in keep_ids]
            self.ec2.terminate(losers)
            terminated += losers
            self.log(f"[round {rno}] kept={[s.candidate.public_ip for s in kept]} terminated={len(terminated)}")
            self._emit("round_done", round=rno,
                       kept=[{"instance_id": s.candidate.instance_id, "public_ip": s.candidate.public_ip,
                              "composite": s.composite} for s in kept],
                       terminated=list(terminated), backend_errors=dict(errors))
            return RoundResult(rno, cands, vetoed, scored, kept, terminated, errors)
        except BaseException:  # 含 KeyboardInterrupt：先终止本轮实例再上抛
            self.ec2.terminate([i for i in ids if i not in terminated])
            raise
