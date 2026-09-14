"""agent 探测：客户中国区服务器（受 SSM 管理）主动向候选 IP 发 ping 与 TCP 连接，方向为 China → AWS。

每台 agent 实例只下发一条 SSM 命令，脚本内遍历全部候选 IP，输出逐包 RTT 与逐次 TCP 连接耗时，
分位数与抖动在本地计算。agent 实例的 isp 标签来自配置 `instances: {instance_id: isp}`。
"""
import json
from concurrent.futures import ThreadPoolExecutor

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend
from crossborder_selector.stats import rtt_summary

MARKER = "CROSSBORDER_AGENT_JSON:"

_SCRIPT_HEAD = r'''#!/bin/bash
# 由 crossborder_selector 生成（agent 探测）。任何单目标失败都不中断。
out=""; sep=""
probe_ip() { # ip ping_count tcp_count ports...
  local ip="$1" n="$2" tc="$3"; shift 3
  local r rtts="" first=1 line ms
  r=$(ping -c "$n" -W 2 -i 0.2 "$ip" 2>/dev/null)
  while read -r line; do
    ms=$(echo "$line" | sed -n 's/.*time=\([0-9.]*\).*/\1/p')
    [ -z "$ms" ] && continue
    if [ $first -eq 1 ]; then rtts="$ms"; first=0; else rtts="$rtts,$ms"; fi
  done <<< "$r"
  local tcp="" tsep=""
  for port in "$@"; do
    local ok_list="" ofirst=1 i t0 t1
    for i in $(seq 1 "$tc"); do
      t0=$(date +%s%N)
      if timeout 3 bash -c "exec 3<>/dev/tcp/$ip/$port" 2>/dev/null; then
        t1=$(date +%s%N)
        if [ $ofirst -eq 1 ]; then ok_list=$(( (t1-t0)/1000000 )); ofirst=0; else ok_list="$ok_list,$(( (t1-t0)/1000000 ))"; fi
      fi
    done
    tcp="$tcp$tsep{\"port\":$port,\"attempts\":$tc,\"connect_ms\":[$ok_list]}"; tsep=","
  done
  out="$out$sep{\"ip\":\"$ip\",\"ping\":{\"sent\":$n,\"rtts\":[$rtts]},\"tcp\":[$tcp]}"; sep=","
}
'''


def build_agent_script(ips: list, ping_count: int, tcp_ports: list, tcp_count: int) -> str:
    ports = " ".join(str(int(p)) for p in tcp_ports)
    lines = [_SCRIPT_HEAD]
    for ip in ips:
        lines.append(f'probe_ip "{ip}" {int(ping_count)} {int(tcp_count)} {ports}')
    lines.append(f'echo "{MARKER}[$out]"')
    return "\n".join(lines) + "\n"


def parse_agent_output(text: str, isp: str) -> dict:
    """返回 {ip: [IspProbe...]}：每个 IP 一条 ping 样本，每个端口一条 tcp 样本。"""
    for line in text.splitlines():
        if not line.startswith(MARKER):
            continue
        items = json.loads(line[len(MARKER):])
        out = {}
        for item in items:
            ip, probes = item["ip"], []
            ping = item.get("ping") or {}
            rtts = [float(x) for x in ping.get("rtts", [])]
            s = rtt_summary(rtts)
            probes.append(IspProbe(isp=isp, sent=int(ping.get("sent") or 0), received=len(rtts),
                                   median_rtt_ms=s["avg"], target=ip, method="ping",
                                   p95_rtt_ms=s["p95"], jitter_ms=s["jitter"]))
            for t in item.get("tcp") or []:
                ms = [float(x) for x in t.get("connect_ms", [])]
                ts = rtt_summary(ms)
                probes.append(IspProbe(isp=isp, sent=int(t.get("attempts") or 0), received=len(ms),
                                       median_rtt_ms=ts["avg"], target=f"{ip}:{t['port']}", method="tcp",
                                       p95_rtt_ms=ts["p95"], jitter_ms=ts["jitter"]))
            out[ip] = probes
        return out
    raise ValueError("agent probe output has no CROSSBORDER_AGENT_JSON marker")


class AgentBackend(ProbeBackend):
    name = "agent"

    def __init__(self, ssm_runner, backend_cfg: dict):
        self.ssm, self.cfg = ssm_runner, backend_cfg
        self.instances = dict(backend_cfg.get("instances") or {})

    def _run_one(self, instance_id, isp, ips):
        script = build_agent_script(ips, self.cfg["ping_count"], self.cfg["tcp_ports"], self.cfg["tcp_count"])
        try:
            status, out = self.ssm.run_script(instance_id, script, timeout_s=self.cfg["timeout_s"])
        except Exception as e:  # 单 agent 的 SSM 调用异常只记错
            return instance_id, isp, {}, f"{type(e).__name__}: {e}"
        if status != "Success":
            return instance_id, isp, {}, f"ssm status {status}: {out[-200:]}"
        try:
            return instance_id, isp, parse_agent_output(out, isp), ""
        except (ValueError, KeyError, TypeError) as e:
            return instance_id, isp, {}, f"parse error: {e}"

    def probe(self, candidates: list) -> dict:
        ips = [c.public_ip for c in candidates]
        merged, errors = {ip: [] for ip in ips}, []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for instance_id, isp, per_ip, err in pool.map(lambda kv: self._run_one(kv[0], kv[1], ips), self.instances.items()):
                if err:
                    errors.append(f"{instance_id}({isp}): {err}")
                    continue
                for ip, probes in per_ip.items():
                    merged.setdefault(ip, []).extend(probes)
        warning = "; ".join(errors)
        out = {}
        for ip in ips:
            probes = merged.get(ip, [])
            if probes:
                # 有可用样本时仍返回 ok；部分 agent 失败记为警告文本（不影响 ok）
                out[ip] = ProbeResult(self.name, probes, "", warning=warning)
            else:
                out[ip] = ProbeResult(self.name, [], warning or "no agent result")
        return out
