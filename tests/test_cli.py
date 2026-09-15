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


def test_probe_source_cidrs_resolution_order():
    from crossborder_selector.cli import probe_source_cidrs
    base = {"enabled": True, "transport": "http", "probe_source_cidrs": []}
    # 1) 显式配置优先
    assert probe_source_cidrs({**base, "probe_source_cidrs": ["10.0.0.0/8"]}, registry={"a": {"ip": "1.1.1.1"}}) == ["10.0.0.0/8"]
    # 2) http：用已注册 agent 的来源 IP（去重、排序、/32）；回环地址对云上候选无意义，丢弃
    reg = {"a": {"ip": "203.0.113.7"}, "b": {"ip": "203.0.113.7"}, "c": {"ip": "127.0.0.1"}, "d": {"ip": ""}}
    assert probe_source_cidrs(base, registry=reg) == ["203.0.113.7/32"]
    # 3) ssm：由调用方传入 agent 实例公网 IP
    assert probe_source_cidrs({**base, "transport": "ssm"}, instance_ips=["198.51.100.9"]) == ["198.51.100.9/32"]
    # 4) s3 或没有任何来源：空列表 → 不开 TCP
    assert probe_source_cidrs({**base, "transport": "s3"}) == []
    assert probe_source_cidrs(base, registry={}) == []
    assert probe_source_cidrs({**base, "enabled": False}, registry=reg) == []


def test_probe_source_cidrs_prefers_public_ip_and_drops_private_or_loopback():
    from crossborder_selector.cli import probe_source_cidrs
    base = {"enabled": True, "transport": "http", "probe_source_cidrs": []}
    reg = {"lap": {"ip": "127.0.0.1", "public_ip": "203.0.113.7"},      # 回环连接但自报公网 IP → 用公网 IP
           "nat": {"ip": "192.168.1.5", "public_ip": ""},               # 内网来源且没有自报 → 丢弃
           "vps": {"ip": "198.51.100.9", "public_ip": ""},              # 公网来源 → 用
           "dup": {"ip": "1.2.3.4", "public_ip": "198.51.100.9"}}       # 与 vps 重复
    assert probe_source_cidrs(base, registry=reg) == ["198.51.100.9/32", "203.0.113.7/32"]
    assert probe_source_cidrs(base, registry={"only": {"ip": "127.0.0.1"}}) == []
    # s3 也能用 registry（来自 S3 心跳）
    assert probe_source_cidrs({**base, "transport": "s3"}, registry={"cn": {"public_ip": "198.51.100.9"}}) == ["198.51.100.9/32"]


def test_select_without_credentials_fails_fast_with_hint(monkeypatch, capsys):
    from botocore.exceptions import NoCredentialsError
    calls = {"infra": 0}
    monkeypatch.setattr(cli, "ensure_infra", lambda *a, **k: calls.__setitem__("infra", calls["infra"] + 1))
    monkeypatch.setenv("AWS_PROFILE", "stale")

    class BadSts:
        def get_caller_identity(self): raise NoCredentialsError()
    factory = lambda cfg: {"ec2": object(), "iam": object(), "ssm": object(), "sts": BadSts()}
    rc = cli.main(["select", "--region", "us-east-1", "--disable-backend", "globalping"], factory=factory)
    cap = capsys.readouterr()
    assert rc == 2 and calls["infra"] == 0
    assert "凭证" in cap.err and "profile=stale" in cap.err and "AWS_PROFILE" in cap.err and "--dry-run" in cap.err
    assert "run-id:" not in cap.out  # 凭证不可用时不生成空的 run


def test_select_prints_identity_when_credentials_ok(monkeypatch, capsys):
    from crossborder_selector.models import Candidate, CandidateScore
    win = CandidateScore(Candidate("i-1", "1.2.3.4"), None, [], {}, {}, 91.0, True)
    class FakeResult: stop_reason, winners = "max_rounds", [win]
    class FakeOrch:
        def __init__(self, *a, **k): pass
        def run(self): return FakeResult()
    monkeypatch.setattr(cli, "ensure_infra", lambda *a, **k: object())
    monkeypatch.setattr(cli, "load_ip_ranges", lambda **k: [])
    monkeypatch.setattr(cli, "Orchestrator", FakeOrch)
    monkeypatch.setattr(cli, "write_reports", lambda *a, **k: {"json": "j", "md": "m", "csv": "c"})
    class Sts:
        def get_caller_identity(self): return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}
    factory = lambda cfg: {"ec2": object(), "iam": object(), "ssm": object(), "sts": Sts()}
    rc = cli.main(["select", "--region", "us-east-1", "--disable-backend", "globalping"], factory=factory)
    cap = capsys.readouterr()
    assert rc == 0 and "arn:aws:iam::123456789012:user/me" in cap.out and "run-id:" in cap.out


def test_default_factory_includes_sts():
    import boto3
    cfg = load_config(None, {"region": "us-east-1"})
    clients = cli.default_factory(cfg)
    assert {"ec2", "iam", "ssm", "sts"} <= set(clients) and clients["sts"].meta.service_model.service_name == "sts"


def test_plan_summary_shows_per_type_keep():
    cfg = load_config(None, {"instance_groups": [{"instance_type": "t4g.micro", "count": 4, "keep": 2},
                                                 {"instance_type": "a1.2xlarge", "count": 1, "keep": 1}]})
    text = cli.plan_summary(cfg, "xb-x")
    assert "keep_top_k=3" in text and "t4g.micro" in text and "keep=2" in text and "a1.2xlarge" in text and "keep=1" in text


def test_estimate_agent_seconds_and_plan_warns_when_timeout_too_short():
    from crossborder_selector.cli import estimate_agent_seconds
    # 20 个目标、8 并发：3 批 × (10×0.2s ping + 2s 余量 + 1 端口×5 次×3s TCP) + 10s 固定开销
    assert estimate_agent_seconds(20, ping_count=10, tcp_ports=[443], tcp_count=5) == 3 * (2 + 2 + 15) + 10
    assert estimate_agent_seconds(1, 10, [443], 5) == 1 * 19 + 10
    cfg = load_config(None, {"batch_size": 20, "backends": {"agent": {"enabled": True, "transport": "http",
                                                                        "timeout_s": 30, "tcp_ports": [443]}}})
    text = cli.plan_summary(cfg, "xb-x")
    assert "agent" in text and "timeout_s=30" in text and "约 67s" in text and "不足" in text
    ok = load_config(None, {"batch_size": 20, "backends": {"agent": {"enabled": True, "transport": "http",
                                                                       "timeout_s": 180, "tcp_ports": [443]}}})
    assert "不足" not in cli.plan_summary(ok, "xb-y")
