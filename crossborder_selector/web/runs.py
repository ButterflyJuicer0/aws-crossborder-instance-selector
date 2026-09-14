"""后台运行管理：线程、事件队列、状态与事件落盘。"""
import glob
import json
import os
import queue
import threading
from dataclasses import dataclass, field, asdict, replace

from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import ensure_infra
from crossborder_selector.aws.ipranges import load_ip_ranges, PrefixLookup
from crossborder_selector.aws.ssm import SsmRunner
from crossborder_selector.cli import build_backends, new_run_id
from crossborder_selector.orchestrator import Orchestrator, utc_now_iso
from crossborder_selector.report import write_reports, load_history
from crossborder_selector.reputation.abuseipdb import build_sources
from crossborder_selector.diagnostics import redact, secret_values

MAX_DETAIL_EVENTS = 200


@dataclass
class RunRecord:
    run_id: str
    state: str
    region: str
    started_at: str
    config: dict
    finished_at: str = ""
    events: list = field(default_factory=list)
    stop_reason: str = ""
    winners: list = field(default_factory=list)
    report_paths: dict = field(default_factory=dict)
    leftover_instance_ids: list | None = None
    error: str = ""

    def to_summary(self) -> dict:
        d = asdict(self)
        d.pop("events")
        return d

    def to_detail(self) -> dict:
        d = asdict(self)
        d["events"] = self.events[-MAX_DETAIL_EVENTS:]
        return d


class RunManager:
    def __init__(self, api, output_dir, orchestrator_factory=None, clock=utc_now_iso, history_file=None):
        self.api, self.output_dir, self.clock = api, output_dir, clock
        self.history_file = history_file
        self.orchestrator_factory = orchestrator_factory or Orchestrator
        self._records, self._subs, self._flags = {}, {}, {}
        self._secrets = {}
        self._lock = threading.Lock()
        os.makedirs(output_dir, exist_ok=True)

    # ---------- 查询 ----------
    def get(self, run_id):
        return self._records.get(run_id)

    def list(self) -> list:
        with self._lock:  # 在锁内快照，避免遍历时 _records 被并发改写
            records = dict(self._records)
        rows = {}
        for path in glob.glob(os.path.join(self.output_dir, "*", "status.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if d.get("state") == "running" and d.get("run_id") not in records:
                d["state"] = "unknown"
            rows[d.get("run_id", os.path.basename(os.path.dirname(path)))] = d
        for rid, rec in records.items():
            rows[rid] = rec.to_summary()
        return sorted(rows.values(), key=lambda d: d.get("started_at", ""), reverse=True)

    def winner_ids(self, run_id) -> list:
        rec = self._records.get(run_id)
        if rec and rec.winners:
            return [w["instance_id"] for w in rec.winners]
        path = os.path.join(self.output_dir, run_id, "report.json")
        try:
            with open(path) as f:
                return [w["instance_id"] for w in json.load(f).get("winners", [])]
        except (OSError, ValueError):
            return []

    # ---------- 事件 ----------
    def subscribe(self, run_id) -> "queue.Queue":
        q = queue.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id, q):
        with self._lock:
            if q in self._subs.get(run_id, []):
                self._subs[run_id].remove(q)

    def emit(self, run_id, event: dict):
        event = redact(dict(event), self._secrets.get(run_id, []))
        event.setdefault("ts", self.clock())
        rec = self._records[run_id]
        with self._lock:
            rec.events.append(event)
            subs = list(self._subs.get(run_id, []))
        with open(os.path.join(self.output_dir, run_id, "events.jsonl"), "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        for q in subs:
            q.put(event)

    def _persist(self, rec: RunRecord):
        d = os.path.join(self.output_dir, rec.run_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(rec.to_summary(), f, ensure_ascii=False, indent=2)

    # ---------- 启动 / 取消 ----------
    def start(self, overrides: dict) -> str:
        cfg = self.api.load(overrides)
        if hasattr(self.api, "preflight"):
            self.api.preflight(cfg)
        run_id = new_run_id()
        # record.config 保留用户的 protect 选择（供 select 时默认勾选），但 Web 运行阶段不加固任何
        # 实例（加固只在 select 对选定实例执行）；history_file 若被注入则改写，避免污染仓库 history/。
        rec = RunRecord(run_id, "running", cfg.region, self.clock(), redact(asdict(cfg)))
        run_cfg = replace(cfg, protect=False)
        if self.history_file:
            run_cfg = replace(run_cfg, history_file=self.history_file)
        self._records[run_id] = rec
        self._secrets[run_id] = secret_values(asdict(cfg))
        self._flags[run_id] = threading.Event()
        self._persist(rec)
        threading.Thread(target=self._run, args=(rec, run_cfg), daemon=True, name=f"run-{run_id}").start()
        return run_id

    def cancel(self, run_id):
        rec = self._records.get(run_id)
        flag = self._flags.get(run_id)
        if rec is None or flag is None or rec.state != "running":
            return False  # 只有仍在运行的 run 才能取消；已结束的返回 False（server 转 404）
        flag.set()
        self.emit(run_id, {"type": "cancelled", "message": "已请求取消，当前轮结束后停止，保留已选出的实例"})
        return True

    def _run(self, rec: RunRecord, cfg):
        run_id, clients = rec.run_id, None
        try:
            clients = self.api.factory(cfg)
            infra = ensure_infra(clients["ec2"], clients["iam"], clients["ssm"], cfg)
            ssm_runner = SsmRunner(clients["ssm"])
            prefixes = load_ip_ranges(cache_path=os.path.join(self.output_dir, "ip-ranges.json"))
            extra = {}
            if cfg.backends["agent"]["enabled"] and cfg.backends["agent"]["transport"] == "http":
                # Web 服务本身已提供 /api/agent 接口，直接共用进程内信箱，不再另起监听器
                from crossborder_selector.probes.agent_transport import shared_store
                extra["agent_broker"] = shared_store()
            ec2_log = lambda m: self.emit(run_id, {"type": "log", "message": str(m)})  # noqa: E731
            orch = self.orchestrator_factory(
                cfg, Ec2Manager(clients["ec2"], log=ec2_log), ssm_runner, build_backends(cfg, ssm_runner, **extra), build_sources(cfg.reputation),
                PrefixLookup(prefixes, cfg.region), infra, run_id,
                log=lambda m: self.emit(run_id, {"type": "log", "message": str(m)}),
                on_event=lambda e: self.emit(run_id, e),
                should_stop=self._flags[run_id].is_set,
                prefix_history=load_history(self.history_file or cfg.history_file))
            result = orch.run()
            # winner 先于报告落库：即便随后 write_reports 失败，winner 已可见且带进 failed 事件
            rec.stop_reason = result.stop_reason
            rec.winners = [{"instance_id": w.candidate.instance_id, "public_ip": w.candidate.public_ip,
                            "prefix": w.candidate.prefix, "composite": w.composite, "round": w.candidate.round,
                            "isp_scores": w.isp_scores} for w in result.winners]
            self._persist(rec)
            paths = write_reports(result, cfg, out_dir=self.output_dir)
            rec.report_paths = {k: v for k, v in paths.items() if k in ("json", "md", "csv")}
            rec.finished_at = self.clock()
            # 先落终态事件（events.jsonl 与 record.events），再翻转 state：worker 线程里 state 必须是
            # 最后写入的字段，主线程一旦观察到 state 为终态，即保证终态事件已在 record.events 中。
            self.emit(run_id, {"type": "finished", "stop_reason": rec.stop_reason, "winners": rec.winners,
                               "report_paths": rec.report_paths})
            rec.state = "cancelled" if result.stop_reason == "cancelled" else "finished"
            self._persist(rec)
        except BaseException as e:  # 线程内任何失败都要落状态并带上遗留实例
            rec.error, rec.finished_at = redact(f"{type(e).__name__}: {e}", self._secrets.get(run_id, [])), self.clock()
            try:
                rec.leftover_instance_ids = Ec2Manager(clients["ec2"]).list_run_instances(run_id) if clients else None
                if clients and not rec.winners:
                    rec.winners = Ec2Manager(clients["ec2"]).list_run_winners(run_id)
            except Exception:
                rec.leftover_instance_ids = None
            # 同上：先落 failed 事件，再翻转 state，保证主线程读到终态时事件已就绪
            self.emit(run_id, {"type": "failed", "message": rec.error, "leftover_instance_ids": rec.leftover_instance_ids,
                               "winner_instance_ids": [w["instance_id"] for w in rec.winners]})
            rec.state = "failed"
            self._persist(rec)
