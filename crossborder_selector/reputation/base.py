from abc import ABC, abstractmethod
from crossborder_selector.models import SourceResult, ReputationResult


class ReputationSource(ABC):
    name: str = "source"
    weight: float = 50.0

    @abstractmethod
    def check(self, address: str) -> SourceResult: ...


def score_reputation(address: str, sources) -> ReputationResult:
    results, score = [], 100.0
    for s in sources:
        try:
            r = s.check(address)
        except Exception as e:  # 单源失败不影响整体，也不扣分
            r = SourceResult(s.name, False, f"error:{e}")
        results.append(r)
        if r.listed:
            score -= s.weight
    return ReputationResult(address=address, results=results, score=max(0.0, score))
