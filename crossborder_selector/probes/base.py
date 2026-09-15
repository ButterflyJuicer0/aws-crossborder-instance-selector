"""探测 backend 接口与并行执行器。"""
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed

from crossborder_selector.models import ProbeResult


class ProbeBackend(ABC):
    name: str = "backend"

    @abstractmethod
    def probe(self, candidates: list) -> dict:
        """返回 {public_ip: ProbeResult}。允许抛异常，由 run_backends 兜底。"""


def run_backends(backends, candidates, max_workers=4, on_done=None):
    """并行跑所有 backend。返回 ({ip: [ProbeResult...]}, {backend: error})。

    on_done(name, error, elapsed_s) 在每个 backend 完成时按完成顺序回调，供上层打进度日志。
    """
    results = {c.public_ip: [] for c in candidates}
    errors = {}

    def one(b):
        t0 = time.monotonic()
        try:
            out, err = b.probe(candidates), ""
        except Exception as e:  # 单 backend 失败只记错，不影响其他
            out, err = {}, f"{type(e).__name__}: {e}"
        return b.name, out, err, time.monotonic() - t0

    finished = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for fut in as_completed([pool.submit(one, b) for b in backends]):
            name, out, err, elapsed = fut.result()
            if on_done:
                on_done(name, err, elapsed)
            finished.append((name, out, err))
    order = {b.name: i for i, b in enumerate(backends)}
    for name, out, err in sorted(finished, key=lambda x: order.get(x[0], 0)):  # 结果顺序保持与 backends 一致
        if err:
            errors[name] = err
        candidate_errors = []
        for c in candidates:
            result = out.get(c.public_ip) or ProbeResult(name, [], err or "no result")
            results[c.public_ip].append(result)
            if not result.ok:
                candidate_errors.append(f"{c.public_ip}: {result.error or 'invalid or empty samples'}")
        if candidate_errors:
            errors[name] = "; ".join(candidate_errors)
    return results, errors
