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
