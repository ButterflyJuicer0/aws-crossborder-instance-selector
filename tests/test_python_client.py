import json
from urllib.error import URLError

import pytest

from examples.api_client import SelectorClient, main


def test_default_command_and_start_without_execute_only_plan(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(SelectorClient, "plan", lambda self, config: calls.append(("plan", config)) or {"config": config})
    monkeypatch.setattr(SelectorClient, "start", lambda self, config: pytest.fail("Unexpected resource creation"))
    assert main([]) == 0
    assert main(["start", "--count", "2", "--rounds", "1"]) == 0
    assert calls == [("plan", {}), ("plan", {"batch_size": 2, "max_rounds": 1})]


def test_explicit_execute_resolves_image_and_preserves_per_instance_config(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"region": "ap-east-1", "instance_type": "t4g.nano", "batch_size": 2,
                               "instance_overrides": [{}, {"root_volume_size_gib": 20}]}))
    monkeypatch.setattr(SelectorClient, "plan", lambda self, config: {"config": config})
    def images(self, region, instance_type):
        assert (region, instance_type) == ("ap-east-1", "t4g.nano")
        return {"images": [{"key": "ubuntu2404", "image_id": "ami-matching"}]}
    monkeypatch.setattr(SelectorClient, "images", images)
    calls = []
    monkeypatch.setattr(SelectorClient, "start", lambda self, config: calls.append(config) or "xb-test")
    assert main(["start", "--config-json", str(path), "--image", "ubuntu2404", "--execute"]) == 0
    assert calls == [{"region": "ap-east-1", "instance_type": "t4g.nano", "batch_size": 2,
                      "instance_overrides": [{}, {"root_volume_size_gib": 20}], "image_id": "", "image_preset": "ubuntu2404"}]
    assert json.loads(capsys.readouterr().out)["run_id"] == "xb-test"


def test_start_connection_failure_does_not_retry(monkeypatch):
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise URLError("connection lost")
    monkeypatch.setattr(SelectorClient, "_request", request)
    with pytest.raises(RuntimeError, match="勿直接重复启动"):
        SelectorClient().start({})
    assert len(calls) == 1


def test_unknown_run_stops_polling(monkeypatch):
    monkeypatch.setattr(SelectorClient, "status", lambda self, run_id: {"state": "unknown"})
    assert SelectorClient().wait("xb-test")["state"] == "unknown"
