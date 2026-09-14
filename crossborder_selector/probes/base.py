"""探测 backend 接口与并行执行器。"""
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from crossborder_selector.models import ProbeResult


class ProbeBackend(ABC):
    name: str = "backend"

    @abstractmethod
    def probe(self, candidates: list) -> dict:
        """返回 {public_ip: ProbeResult}。允许抛异常，由 run_backends 兜底。"""


def run_backends(backends, candidates, max_workers=4):
    """并行跑所有 backend。返回 ({ip: [ProbeResult...]}, {backend: error})。"""
    results = {c.public_ip: [] for c in candidates}
    errors = {}

    def one(b):
        try:
            return b.name, b.probe(candidates), ""
        except Exception as e:  # 单 backend 失败只记错，不影响其他
            return b.name, {}, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, out, err in pool.map(one, backends):
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
