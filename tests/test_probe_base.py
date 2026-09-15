import pytest
from crossborder_selector.models import Candidate, IspProbe, ProbeResult
from crossborder_selector.probes.base import ProbeBackend, run_backends

C = [Candidate("i-1", "1.1.1.1"), Candidate("i-2", "2.2.2.2")]


class Good(ProbeBackend):
    name = "good"
    def probe(self, candidates):
        return {c.public_ip: ProbeResult("good", [IspProbe("telecom", 4, 4, 50.0)]) for c in candidates}


class Boom(ProbeBackend):
    name = "boom"
    def probe(self, candidates):
        raise RuntimeError("api down")


def test_abstract():
    with pytest.raises(TypeError):
        ProbeBackend()


def test_run_backends_collects_and_isolates_errors():
    results, errors = run_backends([Good(), Boom()], C)
    assert set(results) == {"1.1.1.1", "2.2.2.2"}
    by_name = {r.backend: r for r in results["1.1.1.1"]}
    assert by_name["good"].ok is True
    assert by_name["boom"].ok is False and "api down" in by_name["boom"].error
    assert "api down" in errors["boom"] and "good" not in errors


def test_run_backends_reports_each_backend_as_it_completes():
    import time
    from crossborder_selector.models import Candidate, ProbeResult, IspProbe
    from crossborder_selector.probes.base import ProbeBackend, run_backends

    class Slow(ProbeBackend):
        def __init__(self, name, delay, fail=False): self.name, self.delay, self.fail = name, delay, fail
        def probe(self, cands):
            time.sleep(self.delay)
            if self.fail: raise RuntimeError("boom")
            return {c.public_ip: ProbeResult(self.name, [IspProbe("HK", 1, 1, 10.0)]) for c in cands}

    done = []
    run_backends([Slow("slow", 0.15), Slow("fast", 0.01), Slow("bad", 0.05, fail=True)], [Candidate("i", "1.1.1.1")],
                 on_done=lambda name, error, elapsed: done.append((name, bool(error), elapsed)))
    assert [d[0] for d in done] == ["fast", "bad", "slow"]          # 按完成顺序回调，而不是提交顺序
    assert [d[1] for d in done] == [False, True, False]
    assert all(d[2] >= 0 for d in done)
