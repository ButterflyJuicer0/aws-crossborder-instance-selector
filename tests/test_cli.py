import json
import re
import boto3
import pytest
from moto import mock_aws

from crossborder_selector import cli
from crossborder_selector.config import load_config

REGION = "us-east-1"


def test_run_id_format():
    assert re.fullmatch(r"xb-\d{8}T\d{6}Z-[0-9a-f]{4}", cli.new_run_id())


def test_build_backends_respects_enable_flags():
    cfg = load_config(None, {"enable_backends": ["itdog"], "disable_backends": ["globalping"]})
    names = [b.name for b in cli.build_backends(cfg, ssm_runner=object())]
    assert names == ["reverse", "itdog"]
    cfg2 = load_config(None, {"backends": {"ripeatlas": {"enabled": True, "api_key": ""}}})
    assert "ripeatlas" not in [b.name for b in cli.build_backends(cfg2, ssm_runner=object())]


def test_dry_run_prints_plan_without_clients(capsys):
    def no_factory(cfg):
        raise AssertionError("dry-run must not create clients")
    rc = cli.main(["select", "--dry-run", "--batch-size", "3", "--region", "ap-east-1"], factory=no_factory)
    out = capsys.readouterr().out
    assert rc == 0 and "DRY-RUN" in out and "ap-east-1" in out and "3 x t3.nano" in out and "reverse" in out


def test_select_rejects_bad_override():
    with pytest.raises(SystemExit):
        cli.main(["select", "--dry-run", "--enable-backend", "nope"])


def test_select_auto_loads_cwd_config_yaml(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("batch_size: 7\n")

    def no_factory(cfg):
        raise AssertionError("dry-run must not create clients")
    rc = cli.main(["select", "--dry-run", "--region", "ap-east-1"], factory=no_factory)
    out = capsys.readouterr().out
    assert rc == 0 and "using config.yaml" in out and "7 x t3.nano" in out


@mock_aws
def test_cleanup_terminates_only_run_and_keeps_infra(capsys):
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    # moto 5 预置 /aws/service/ 保留公共参数且禁止写入，ensure_infra 会直接读取其默认 AMI 值
    from crossborder_selector.aws.infra import ensure_infra, PING_SG_NAME as SG_NAME
    from crossborder_selector.aws.ec2 import Ec2Manager
    cfg = load_config(None, {"region": REGION})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    a = m.launch(2, "xb-a", 1, infra, "t3.nano")
    b = m.launch(1, "xb-b", 1, infra, "t3.nano")
    factory = lambda cfg: {"ec2": ec2, "iam": iam, "ssm": ssm}
    assert cli.main(["cleanup", "--region", REGION, "--run-id", "xb-a"], factory=factory) == 0
    assert m.list_run_instances("xb-a") == [] and set(m.list_run_instances("xb-b")) == set(b)
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]
    assert cli.main(["cleanup", "--region", REGION, "--run-id", "xb-b", "--include-infra"], factory=factory) == 0
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"] == []


def test_select_report_failure_still_prints_winners(monkeypatch, capsys):
    from crossborder_selector.models import Candidate, CandidateScore
    win = CandidateScore(Candidate("i-1", "1.2.3.4", prefix="1.2.0.0/16"), None, [], {}, {}, 91.0, True)

    class FakeResult:
        stop_reason = "max_rounds"
        winners = [win]

    class FakeOrch:
        def __init__(self, *a, **k): pass
        def run(self): return FakeResult()

    monkeypatch.setattr(cli, "ensure_infra", lambda *a, **k: object())
    monkeypatch.setattr(cli, "load_ip_ranges", lambda **k: [])
    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(cli, "write_reports", boom)

    factory = lambda cfg: {"ec2": object(), "iam": object(), "ssm": object()}
    rc = cli.main(["select", "--region", "us-east-1", "--disable-backend", "globalping"], factory=factory)
    cap = capsys.readouterr()
    assert rc == 0
    assert "WINNER i-1 1.2.3.4" in cap.out and "run-id:" in cap.out
    assert "report failed: disk full" in cap.err


def test_report_regenerates(tmp_path, capsys):
    d = {"run_id": "xb-r", "region": "r", "started_at": "", "finished_at": "", "stop_reason": "x", "rounds_completed": 0,
         "config": {}, "winners": [], "rounds": [], "candidates": [], "prefixes": {}}
    out = tmp_path / "out" / "xb-r"
    out.mkdir(parents=True)
    (out / "report.json").write_text(json.dumps(d))
    assert cli.main(["report", "--run-id", "xb-r", "--output-dir", str(tmp_path / "out")]) == 0
    assert (out / "report.md").exists() and (out / "candidates.csv").exists()


def test_build_backends_adds_agent_with_injected_runner():
    from crossborder_selector.probes.agent import AgentBackend
    cfg = load_config(None, {"backends": {"agent": {"enabled": True, "region": "cn-north-1", "profile": "cn",
                                                   "instances": {"i-0123456789abcdef0": "telecom"}}}})
    sentinel = object()
    backends = cli.build_backends(cfg, ssm_runner=None, agent_ssm=sentinel)
    agents = [b for b in backends if isinstance(b, AgentBackend)]
    assert len(agents) == 1 and agents[0].ssm is sentinel
    # 未启用时不创建 agent 探测源，也不需要额外凭证
    assert not any(b.name == "agent" for b in cli.build_backends(load_config(None), ssm_runner=None))


def test_build_backends_remote_transports_use_injected_broker():
    from crossborder_selector.probes.agent_transport import RemoteAgentBackend
    for over in ({"transport": "http", "http": {"listen": "127.0.0.1:0", "token": "t"}},
                 {"transport": "s3", "s3": {"bucket": "b", "prefix": "p"}}):
        cfg = load_config(None, {"backends": {"agent": {"enabled": True, **over}}})
        broker = object()
        backends = cli.build_backends(cfg, ssm_runner=None, agent_broker=broker)
        remote = [b for b in backends if isinstance(b, RemoteAgentBackend)]
        assert len(remote) == 1 and remote[0].broker is broker
