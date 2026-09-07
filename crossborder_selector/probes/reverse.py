"""反向探测：候选机经 SSM 向三网目标 ping + tcping，输出一行 JSON。"""
import json
from concurrent.futures import ThreadPoolExecutor

from crossborder_selector.models import IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend

MARKER = "CROSSBORDER_JSON:"

_SCRIPT_HEAD = r'''#!/bin/bash
# 由 crossborder_selector 生成。任何单目标失败都不中断。
out=""; sep=""
emit() { out="$out$sep{\"isp\":\"$1\",\"target\":\"$2\",\"method\":\"$3\",\"sent\":$4,\"received\":$5,\"avg_ms\":${6:-null}}"; sep=","; }
probe_ping() { # isp target count
  local r tx rx avg
  r=$(ping -c "$3" -W 2 -i 0.2 "$2" 2>/dev/null)
  tx=$(echo "$r" | sed -n 's/^\([0-9]*\) packets transmitted.*/\1/p'); [ -z "$tx" ] && tx=$3
  rx=$(echo "$r" | sed -n 's/^[0-9]* packets transmitted, \([0-9]*\) .*received.*/\1/p'); [ -z "$rx" ] && rx=0
  avg=$(echo "$r" | sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p')
  emit "$1" "$2" "ping" "$tx" "$rx" "$avg"
}
probe_tcp() { # isp host port count
  local ok=0 total=0 i t0 t1 avg=""
  for i in $(seq 1 "$4"); do
    t0=$(date +%s%N)
    if timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then
      t1=$(date +%s%N); ok=$((ok+1)); total=$((total+(t1-t0)/1000000))
    fi
  done
  [ "$ok" -gt 0 ] && avg=$((total/ok))
  emit "$1" "$2:$3" "tcp" "$4" "$ok" "$avg"
}
'''


def split_target(entry: str, default_port: int):
    """目标项可为 host 或 host:port。ping 只用 host 部分；tcping 用给定端口，否则用默认端口。"""
    host, sep, port = entry.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return entry, int(default_port)


def build_script(targets: dict, ping_count: int, tcping_count: int, tcping_port: int) -> str:
    lines = [_SCRIPT_HEAD]
    for isp, hosts in targets.items():
        for entry in hosts:
            host, port = split_target(entry, tcping_port)
            lines.append(f'probe_ping "{isp}" "{host}" {int(ping_count)}')
            lines.append(f'probe_tcp "{isp}" "{host}" {port} {int(tcping_count)}')
    lines.append(f'echo "{MARKER}[$out]"')
    return "\n".join(lines) + "\n"


def parse_output(text: str) -> list:
    for line in text.splitlines():
        if line.startswith(MARKER):
            items = json.loads(line[len(MARKER):])
            return [IspProbe(isp=i["isp"], sent=int(i["sent"]), received=int(i["received"]),
                             median_rtt_ms=(None if i.get("avg_ms") is None else float(i["avg_ms"])),
                             target=i.get("target", ""), method=i.get("method", ""))
                    for i in items]
    raise ValueError("reverse probe output has no CROSSBORDER_JSON marker")


class ReverseBackend(ProbeBackend):
    name = "reverse"

    def __init__(self, ssm_runner, backend_cfg: dict):
        self.ssm = ssm_runner
        self.cfg = backend_cfg
        self.script = build_script(backend_cfg["targets"], backend_cfg["ping_count"],
                                   backend_cfg["tcping_count"], backend_cfg["tcping_port"])

    def _one(self, c):
        if not c.ssm_online:
            return c.public_ip, ProbeResult(self.name, [], "ssm offline")
        status, out = self.ssm.run_script(c.instance_id, self.script, timeout_s=self.cfg["timeout_s"])
        if status != "Success":
            return c.public_ip, ProbeResult(self.name, [], f"ssm status {status}: {out[-200:]}")
        try:
            return c.public_ip, ProbeResult(self.name, parse_output(out))
        except (ValueError, KeyError) as e:
            return c.public_ip, ProbeResult(self.name, [], f"parse error: {e}")

    def probe(self, candidates: list) -> dict:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(self._one, candidates))
