"""审查发现的失败路径回归；AWS、HTTP 和 WebSocket 均使用模拟对象。"""
import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import quote

import boto3
import dns.resolver
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

from crossborder_selector.aws.ec2 import Ec2Manager, RUN_TAG
from crossborder_selector.aws.infra import (
    ensure_infra, ensure_instance_profile, delete_shared_iam, validate_security_group,
    PROFILE_NAME, ROLE_NAME, SG_NAME,
)
from crossborder_selector.config import load_config
from crossborder_selector.models import Candidate, IspProbe, ProbeResult, RoundResult, RunResult
from crossborder_selector.probes.base import run_backends
from crossborder_selector.probes.itdog import ItdogBackend
from crossborder_selector.report import to_dict
from crossborder_selector.reputation.base import score_reputation
from crossborder_selector.reputation.dnsbl import DnsblSource
from crossborder_selector.scoring import score_candidate
from crossborder_selector.web.api import Api, ApiError
from crossborder_selector.web.server import Handler
from crossborder_selector.web.pricing import estimate
from tests.test_probe_itdog import FakeClock, FakeSession, FakeWs
from tests.test_web_api import _factory, _select_api, _err


@pytest.mark.parametrize("overrides", [
    {"min_backends": 0}, {"min_backends": 3}, {"batch_size": 51},
    {"max_rounds": 1.5}, {"target_score": float("nan")},
    {"disable_backends": ["reverse", "globalping"]},
    {"weights": {"isps": {"telecom": -1}}},
    {"weights": {"backends": {"reverse": 0, "globalping": 0}}},
    {"backends": {"reverse": {"timeout_s": 0}}},
    {"backends": {"reverse": {"targets": {"telecom": ['host"; echo unsafe']}}}},
])
def test_invalid_plan_rejected_before_resources(overrides):
    with pytest.raises(ValueError):
        load_config(overrides=overrides)


@pytest.mark.parametrize("reverse,enabled,reason", [
    (ProbeResult("reverse", [], "ssm offline"), True, "reverse_unavailable"),
    (ProbeResult("reverse", [IspProbe("telecom", 4, 4, 20)]), True, "reverse_incomplete"),
    (None, False, "unreachable"),
])
def test_no_usable_response_or_required_coverage_is_not_a_winner(reverse, enabled, reason):
    cfg = load_config()
    results = [ProbeResult("globalping", [IspProbe("HK", 4, 0, None)])]
    if reverse:
        results.append(reverse)
    score = score_candidate(Candidate("i-a", "192.0.2.1"), None, results, cfg.weights, 1, enabled)
    assert not score.qualified and score.veto_reason == reason


def test_zero_packets_do_not_count_as_a_completed_source():
    assert not ProbeResult("globalping", [IspProbe("HK", 0, 0)]).ok


@mock_aws
def test_default_public_ping_and_reverse_only_groups_are_separate():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    iam = boto3.client("iam", region_name="us-east-1")
    ssm = boto3.client("ssm", region_name="us-east-1")
    cfg = load_config(overrides={"region": "us-east-1"})
    external = ensure_infra(ec2, iam, ssm, cfg)
    reverse = ensure_infra(ec2, iam, ssm, load_config(overrides={
        "region": "us-east-1", "disable_backends": ["globalping"]}))
    assert external.security_group_id != reverse.security_group_id
    group = ec2.describe_security_groups(GroupIds=[external.security_group_id])["SecurityGroups"][0]
    assert len(group["IpPermissions"]) == 1
    rule = group["IpPermissions"][0]
    assert (rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("icmp", 8, 0)
    assert rule["IpRanges"][0]["CidrIp"] == "0.0.0.0/0"
    old_group = ec2.describe_security_groups(GroupIds=[reverse.security_group_id])["SecurityGroups"][0]
    assert old_group["GroupName"] == SG_NAME and old_group["IpPermissions"] == []
    with pytest.raises(RuntimeError, match="external ping"):
        validate_security_group(ec2, reverse.security_group_id, old_group["VpcId"], True)


@mock_aws
def test_failed_winner_tag_write_preserves_cleanup_ownership(monkeypatch):
    from tests.test_ec2 import _setup, _tags
    ec2, infra = _setup()
    manager = Ec2Manager(ec2)
    iid = manager.launch(1, "xb-failure", 1, infra, "t3.nano")[0]
    def fail(**kwargs):
        raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "test"}}, "CreateTags")
    monkeypatch.setattr(ec2, "create_tags", fail)
    with pytest.raises(ClientError):
        manager.mark_winner(iid, "xb-failure", 90, 1, "now")
    assert _tags(ec2, iid)[RUN_TAG] == "xb-failure"
    assert manager.list_run_instances("xb-failure") == [iid]


def _iam_setup(monkeypatch):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    iam = boto3.client("iam", region_name="us-east-1")
    ensure_instance_profile(iam)
    monkeypatch.setattr(ec2, "describe_regions", lambda **kw: {
        "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]})
    return ec2, iam


@mock_aws
def test_shared_iam_cleanup_rejects_another_region_dependency(monkeypatch):
    ec2, iam = _iam_setup(monkeypatch)
    remote = boto3.client("ec2", region_name="us-west-2")
    profile = iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]
    expected = {"Filters": [
        {"Name": "iam-instance-profile.arn", "Values": [profile["Arn"]]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]}
    # Moto cannot filter populated instance lists by profile ARN; Stubber verifies the real API request.
    with Stubber(ec2) as local_stub, Stubber(remote) as remote_stub:
        local_stub.add_response("describe_instances", {"Reservations": []}, expected)
        remote_stub.add_response("describe_instances", {"Reservations": [{"Instances": [
            {"InstanceId": "i-12345678", "IamInstanceProfile": {"Arn": profile["Arn"]}}]}]}, expected)
        with pytest.raises(RuntimeError, match="us-west-2"):
            delete_shared_iam(ec2, iam, lambda region: ec2 if region == "us-east-1" else remote)
        local_stub.assert_no_pending_responses()
        remote_stub.assert_no_pending_responses()
    assert iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]["Roles"]


@mock_aws
def test_unused_owned_iam_can_be_deleted_after_all_region_checks(monkeypatch):
    ec2, iam = _iam_setup(monkeypatch)
    delete_shared_iam(ec2, iam, lambda region: boto3.client("ec2", region_name=region))
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)


@mock_aws
def test_iam_dependency_query_failure_does_not_mutate_resources(monkeypatch):
    ec2, iam = _iam_setup(monkeypatch)
    def denied(region):
        raise _err("UnauthorizedOperation")
    with pytest.raises(ClientError):
        delete_shared_iam(ec2, iam, denied)
    assert iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]["Roles"]


@mock_aws
def test_unmanaged_iam_is_not_deleted(monkeypatch):
    ec2, iam = _iam_setup(monkeypatch)
    iam.untag_role(RoleName=ROLE_NAME, TagKeys=["crossborder-managed"])
    with pytest.raises(RuntimeError, match="unmanaged IAM"):
        delete_shared_iam(ec2, iam, lambda region: None)
    assert iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]["Roles"]


def test_itdog_idle_receive_waits_for_later_sample_and_http_has_timeout():
    clock = FakeClock()
    class Session(FakeSession):
        def post(self, url, **kwargs):
            assert 0 < kwargs["timeout"][0] <= 10
            assert kwargs["timeout"][1] == 30
            return super().post(url, **kwargs)
    class Stream(FakeWs):
        def recv(self, timeout=None):
            clock.tick(2)
            if clock.t == 2:
                raise TimeoutError("idle")
            return super().recv(timeout)
    ws = Stream([
        json.dumps({"type": "data", "ip": "192.0.2.1", "node_id": "1310", "result": "20"}),
        json.dumps({"type": "finished"}),
    ])
    cfg = load_config().backends["itdog"]
    result = ItdogBackend(cfg, session=Session(), ws_connect=lambda url: ws, clock=clock).probe([
        Candidate("i-a", "192.0.2.1")])["192.0.2.1"]
    assert result.ok and result.probes[0].received == 1
    assert clock.t == 6


@pytest.mark.parametrize("failure,expected", [
    (dns.resolver.NXDOMAIN(), "clear"),
    (TimeoutError("DNS timeout"), "unknown"),
    (dns.resolver.NoNameservers(), "unknown"),
])
def test_dns_not_listed_is_distinct_from_lookup_failure(failure, expected):
    class Resolver:
        def resolve(self, *args):
            raise failure
    result = score_reputation("192.0.2.1", [DnsblSource(["example.invalid"], resolver=Resolver())])
    assert result.status == expected
    assert (result.score is None) == (expected == "unknown")


def test_report_preserves_samples_and_per_candidate_errors_without_secrets():
    secret = "sensitive+review/key"
    cfg = load_config(overrides={"backends": {"ripeatlas": {"api_key": secret}}})
    c = Candidate("i-a", "192.0.2.1")
    class Backend:
        name = "ripeatlas"
        def probe(self, candidates):
            return {c.public_ip: ProbeResult(self.name, [], f"429 at https://example.invalid?key={quote(secret, safe='')}")}
    results, errors = run_backends([Backend()], [c])
    results[c.public_ip].append(ProbeResult("reverse", [
        IspProbe(i, 10, 8, 60, "example.invalid", "ping") for i in ("telecom", "unicom", "mobile")]))
    score = score_candidate(c, None, results[c.public_ip], cfg.weights, 1, True)
    run = RunResult("xb-test", cfg.region, [RoundResult(1, [c], [], [score], [score], [], errors)],
                    [score], "start", "end", "max_rounds")
    report = to_dict(run, cfg)
    text = json.dumps(report)
    assert secret not in text and quote(secret, safe="") not in text
    assert "429" in report["rounds"][0]["backend_errors"]["ripeatlas"]
    probe = report["candidates"][0]["probe_results"][1]["probes"][0]
    assert probe["sent"] == 10 and probe["received"] == 8 and probe["loss"] == .2
    assert probe["mean_rtt_ms"] == 60 and probe["method"] == "ping"


def test_stop_protection_denial_does_not_report_success_or_terminate_others(tmp_path, monkeypatch):
    api, ec2 = _select_api(tmp_path)
    original = ec2.modify_instance_attribute
    def modify(**kw):
        if "DisableApiStop" in kw:
            raise _err("UnauthorizedOperation")
        return original(**kw)
    monkeypatch.setattr(ec2, "modify_instance_attribute", modify)
    with pytest.raises(ApiError, match="未全部设置成功"):
        api.select("xb-test", "i-a", True, True, "ap-east-1", ["i-a", "i-b"])
    assert not any(name == "terminate" for name, _ in ec2.calls)


@pytest.mark.parametrize("origin,accepted", [
    ("http://192.0.2.1:8765", True),
    ("http://192.0.2.1:8766", False),
    ("https://192.0.2.1:8765", False),
    ("http://example.invalid:8765", False),
])
def test_remote_post_checks_exact_origin(origin, accepted):
    handler = Handler.__new__(Handler)
    handler.server = SimpleNamespace(ctx={"allow_remote": True})
    handler.headers = {"Host": "192.0.2.1:8765", "Origin": origin, "Content-Type": "application/json"}
    if accepted:
        handler._guard("POST")
    else:
        with pytest.raises(ApiError):
            handler._guard("POST")


def test_preflight_checks_selected_batch_and_incumbents(tmp_path):
    api = Api(factory=_factory(quotas={"value": 20}), cwd=str(tmp_path))
    assert api.env("ap-east-1")["ok"]  # User can still enter configuration and reduce the batch.
    with pytest.raises(ApiError, match="配额不足"):
        api.preflight(load_config())  # 10 candidates plus one incumbent require 22 vCPUs.
    api.preflight(load_config(overrides={"batch_size": 2}))
    too_small = Api(factory=_factory(quotas={"value": 1}), cwd=str(tmp_path))
    assert too_small.capacity(too_small.load({}))["problems"]


def test_estimate_reflects_serial_sources_and_does_not_reuse_hk_price_for_other_regions():
    small = estimate("t3.nano", 2, 1)
    large = estimate("t3.nano", 20, 1)
    assert large["estimated_minutes_range"][1] > small["estimated_minutes_range"][1]
    sources = deepcopy(load_config().backends)
    sources["ripeatlas"].update(enabled=True, api_key="placeholder")
    assert estimate("t3.nano", 20, 1, backends=sources)["estimated_minutes"] > large["estimated_minutes"]
    assert estimate("t3.nano", 20, 1, region="us-west-2")["estimated_cost_usd"] is None
