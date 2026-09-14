import pytest
from botocore.exceptions import ClientError

from crossborder_selector.web.api import Api, ApiError, redact
from crossborder_selector.web.demo import _DemoEc2, _DemoSsm


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeSts:
    def get_caller_identity(self): return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/me"}


class FakeQuotas:
    def __init__(self, value=64.0, fail=False): self.value, self.fail = value, fail
    def get_service_quota(self, ServiceCode, QuotaCode):
        if self.fail: raise _err("AccessDeniedException")
        assert (ServiceCode, QuotaCode) == ("ec2", "L-1216C47A")
        return {"Quota": {"Value": self.value}}


class FakeEc2(_DemoEc2):
    def __init__(self, default_vpc=True, offerings=("t3.nano", "t3.micro", "t4g.nano"), winners=()):
        self.default_vpc, self.offerings, self.winners = default_vpc, offerings, winners
    def describe_vpcs(self, Filters):
        return {"Vpcs": [{"VpcId": "vpc-1"}] if self.default_vpc else []}
    def describe_subnets(self, **kwargs):
        return {"Subnets": [{"SubnetId": "s-1", "VpcId": "vpc-1", "AvailabilityZone": "ap-east-1a"},
                            {"SubnetId": "s-2", "VpcId": "vpc-1", "AvailabilityZone": "ap-east-1b"}]}
    def describe_instances(self, Filters):
        for f in Filters:
            if f["Name"] == "tag:crossborder-winner":
                return {"Reservations": [{"Instances": [{"InstanceId": w, "PublicIpAddress": "1.1.1.1", "InstanceType": "t3.nano",
                                                          "Tags": [{"Key": "crossborder-score", "Value": "91.2"}]} for w in self.winners]}]}
        return {"Reservations": [{"Instances": [{"InstanceId": "i-r1", "InstanceType": "t3.nano"},
                                                {"InstanceId": "i-r2", "InstanceType": "t3.nano"}]}]}
    def describe_instance_type_offerings(self, LocationType, Filters):
        return {"InstanceTypeOfferings": [{"InstanceType": t, "Location": "ap-east-1a"} for t in self.offerings]}


def _factory(**kw):
    clients = {"sts": FakeSts(), "ec2": FakeEc2(**kw.get("ec2", {})), "service-quotas": FakeQuotas(**kw.get("quotas", {})),
               "iam": object(), "ssm": _DemoSsm()}
    return lambda cfg: clients


def test_env_ok(tmp_path):
    api = Api(factory=_factory(ec2={"winners": ["i-w1"]}), cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["caller"]["account"] == "123456789012" and e["default_vpc"] == {"present": True, "subnets": 2}
    capacity = api.capacity(api.load({}))
    assert capacity["vcpu_quota"] == 64.0 and capacity["used_vcpus"] == 4
    assert e["config_yaml_present"] is False and e["ok"] is True


def test_env_no_default_vpc_and_quota_denied(tmp_path):
    (tmp_path / "config.yaml").write_text("region: ap-east-1\n")
    api = Api(factory=_factory(ec2={"default_vpc": False}, quotas={"fail": True}), cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["default_vpc"]["present"] is False
    assert api.capacity(api.load({}))["vcpu_quota"] is None
    assert e["config_yaml_present"] is True and e["ok"] is False and "默认 VPC" in e["problems"][0]


def test_env_sts_failure_is_reported_not_raised(tmp_path):
    class BadSts:
        def get_caller_identity(self): raise _err("ExpiredToken")
    f = _factory(); clients = f(None); clients["sts"] = BadSts()
    api = Api(factory=lambda cfg: clients, cwd=str(tmp_path))
    e = api.env("ap-east-1")
    # STS 失败时仍返回 profile 名（便于判断是过期还是选错账户），只是账户/ARN 为空
    assert e["ok"] is False and e["caller"]["account"] is None and e["caller"]["arn"] is None
    assert e["caller"]["profile"] and any("凭证" in p for p in e["problems"])


def test_env_factory_failure_is_reported_not_raised(tmp_path):
    from botocore.exceptions import ProfileNotFound
    def bad_factory(cfg):
        raise ProfileNotFound(profile="x")
    e = Api(factory=bad_factory, cwd=str(tmp_path)).env("ap-east-1")
    assert e["ok"] is False and e["caller"]["account"] is None
    assert any("aws configure" in p for p in e["problems"])


def test_options_uses_aws_offerings(tmp_path):
    api = Api(factory=_factory(ec2={"offerings": ("t3.nano", "t4g.nano")}), cwd=str(tmp_path))
    o = api.options("ap-east-1")
    assert [i["type"] for i in o["instance_types"]] == ["t3.nano", "t4g.nano"]
    assert o["instance_types_source"] == "aws"
    names = {b["name"] for b in o["backends"]}
    assert names == {"reverse", "globalping", "ripeatlas", "itdog", "agent"}
    rip = next(b for b in o["backends"] if b["name"] == "ripeatlas")
    assert rip["needs_key"] is True and rip["key_present"] is False
    assert o["defaults"]["batch_size"] == 10 and "abuseipdb_api_key" not in str(o["defaults"])


def test_options_reports_unavailable_when_offerings_fail(tmp_path):
    class NoOffer(FakeEc2):
        def describe_instance_type_offerings(self, **kw): raise _err("UnauthorizedOperation")
    f = _factory(); clients = f(None); clients["ec2"] = NoOffer()
    o = Api(factory=lambda cfg: clients, cwd=str(tmp_path)).options("ap-east-1")
    assert not o["instance_types"] and o["instance_types_source"] == "unavailable"
    assert "UnauthorizedOperation" in o["errors"][0]


def test_plan_summary_and_cost(tmp_path):
    api = Api(factory=_factory(), cwd=str(tmp_path))
    p = api.plan({"region": "ap-east-1", "batch_size": 4, "max_rounds": 2, "instance_type": "t3.nano"})
    assert "4 x t3.nano" in p["plan_summary"]
    assert p["estimated_minutes_range"][1] > p["estimated_minutes_range"][0]
    assert p["estimated_cost_range_usd"][1] >= p["estimated_cost_range_usd"][0] > 0
    assert p["config"]["batch_size"] == 4 and "api_key" not in str(p["config"])


def test_plan_rejects_bad_override(tmp_path):
    with pytest.raises(ApiError) as ei:
        Api(factory=_factory(), cwd=str(tmp_path)).plan({"batch_size": 0})
    assert ei.value.status == 400


def test_redact_drops_secret_keys_recursively():
    assert redact({"a": {"api_key": "x", "b": 1}, "api_token": "y"}) == {"a": {"b": 1}}


class FakeSelectEc2:
    """记录 modify_instance_attribute / terminate_instances 调用；可让终止抛错。"""
    def __init__(self, terminate_error=None):
        self.calls, self.terminate_error = [], terminate_error
    def modify_instance_attribute(self, **kw):
        self.calls.append(("modify", kw)); return {}
    def terminate_instances(self, **kw):
        self.calls.append(("terminate", kw))
        if self.terminate_error:
            raise self.terminate_error
        return {}


def _select_api(tmp_path, terminate_error=None):
    ec2 = FakeSelectEc2(terminate_error)
    clients = {"sts": FakeSts(), "ec2": ec2, "service-quotas": FakeQuotas(), "iam": object(), "ssm": object()}
    return Api(factory=lambda cfg: clients, cwd=str(tmp_path)), ec2


def test_select_protects_winner_unprotects_then_terminates_others(tmp_path):
    api, ec2 = _select_api(tmp_path)
    r = api.select("xb-r", "i-a", True, True, "ap-east-1", ["i-a", "i-b", "i-c"])
    assert r == {"selected": "i-a", "protected": True, "protection": {"stop": True, "termination": True}, "terminated": ["i-b", "i-c"]}
    modifies = [kw for tag, kw in ec2.calls if tag == "modify"]
    assert {"InstanceId": "i-a", "DisableApiTermination": {"Value": True}} in modifies
    assert {"InstanceId": "i-a", "DisableApiStop": {"Value": True}} in modifies
    assert {"InstanceId": "i-b", "DisableApiTermination": {"Value": False}} in modifies
    assert {"InstanceId": "i-b", "DisableApiStop": {"Value": False}} in modifies
    assert {"InstanceId": "i-c", "DisableApiTermination": {"Value": False}} in modifies
    assert [kw for tag, kw in ec2.calls if tag == "terminate"] == [{"InstanceIds": ["i-b", "i-c"]}]


def test_select_terminate_failure_returns_409(tmp_path):
    api, _ = _select_api(tmp_path, terminate_error=_err("OperationNotPermitted"))
    with pytest.raises(ApiError) as ei:
        api.select("xb-r", "i-a", False, True, "ap-east-1", ["i-a", "i-b"])
    assert ei.value.status == 409 and "i-b" in ei.value.message


def test_terminate_others_helper_unprotects_and_terminates(tmp_path):
    api, ec2 = _select_api(tmp_path)
    r = api.terminate_others("i-a", "ap-east-1", ["i-a", "i-b", "i-c"])
    assert r == {"terminated": ["i-b", "i-c"]}
    assert [kw for tag, kw in ec2.calls if tag == "terminate"] == [{"InstanceIds": ["i-b", "i-c"]}]


def test_env_reports_aws_profile_in_use(tmp_path, monkeypatch):
    api = Api(factory=_factory(), cwd=str(tmp_path))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    e = api.env("ap-east-1")
    assert e["caller"]["profile"] == "default" and e["caller"]["profile_source"] == "默认凭证链"
    monkeypatch.setenv("AWS_PROFILE", "cn")
    e = api.env("ap-east-1")
    assert e["caller"]["profile"] == "cn" and e["caller"]["profile_source"] == "AWS_PROFILE"
    assert e["caller"]["account"] == "123456789012" and e["caller"]["arn"].endswith(":user/me")


def test_env_profile_shown_even_when_sts_fails(tmp_path, monkeypatch):
    class BadSts:
        def get_caller_identity(self): raise _err("ExpiredToken")
    monkeypatch.setenv("AWS_PROFILE", "stale")
    api = Api(factory=lambda cfg: {"sts": BadSts(), "ec2": FakeEc2(), "service-quotas": FakeQuotas(), "iam": object(), "ssm": _DemoSsm()},
              cwd=str(tmp_path))
    e = api.env("ap-east-1")
    assert e["ok"] is False and e["caller"] == {"profile": "stale", "profile_source": "AWS_PROFILE", "account": None, "arn": None}
