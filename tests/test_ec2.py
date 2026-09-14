import boto3
from moto import mock_aws

from crossborder_selector.aws.ec2 import Ec2Manager, RUN_TAG, ROUND_TAG, WINNER_TAG, SCORE_TAG
from crossborder_selector.aws.infra import Infra, ensure_instance_profile

REGION = "us-east-1"


def _setup():
    ec2 = boto3.client("ec2", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="g", Description="d", VpcId=vpc)["GroupId"]
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    profile = ensure_instance_profile(iam)
    return ec2, Infra(subnet, sg, profile, ami)


def _tags(ec2, iid):
    r = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return {t["Key"]: t["Value"] for t in r.get("Tags", [])}


@mock_aws
def test_launch_tags_and_public_ips():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    ids = m.launch(3, "xb-run1", 2, infra, "t3.nano")
    # moto 只启动 MinCount 台（=1），真实 AWS 会启动到 MaxCount；两者都合法
    assert 1 <= len(ids) <= 3
    m.wait_running(ids)
    ips = m.public_ips(ids)
    assert set(ips) == set(ids) and all(ips.values())
    assert _tags(ec2, ids[0]) == {RUN_TAG: "xb-run1", ROUND_TAG: "2"}


@mock_aws
def test_list_and_terminate_only_this_run():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    a = m.launch(2, "xb-a", 1, infra, "t3.nano")
    b = m.launch(1, "xb-b", 1, infra, "t3.nano")
    assert set(m.list_run_instances("xb-a")) == set(a)
    m.terminate(a)
    assert m.list_run_instances("xb-a") == []
    assert set(m.list_run_instances("xb-b")) == set(b)
    m.terminate([])  # 空列表不报错


@mock_aws
def test_mark_winner_preserves_ownership_and_cleanup_excludes_it():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    (iid,) = m.launch(1, "xb-w", 1, infra, "t3.nano")
    assert m.has_winners() is False
    m.mark_winner(iid, "xb-w", 93.456, 2, "2026-09-07T00:00:00Z")
    t = _tags(ec2, iid)
    assert t[RUN_TAG] == "xb-w"
    assert t[WINNER_TAG] == "true" and t[SCORE_TAG] == "93.5" and t["crossborder-round"] == "2"
    assert m.list_run_instances("xb-w") == [] and m.has_winners() is True


def test_mark_winner_sets_shutdown_behavior_to_stop():
    # moto 不回读 instanceInitiatedShutdownBehavior，用假客户端断言调用内容与顺序
    class FakeEc2:
        def __init__(self):
            self.order, self.shutdown = [], None
        def modify_instance_attribute(self, **kw):
            self.order.append("modify"); self.shutdown = kw
        def delete_tags(self, **kw):
            self.order.append("delete_tags")
        def create_tags(self, **kw):
            self.order.append("create_tags")
    fake = FakeEc2()
    Ec2Manager(fake, sleeper=lambda s: None).mark_winner("i-1", "xb-w", 90.0, 1, "2026-09-07T00:00:00Z")
    assert fake.shutdown == {"InstanceId": "i-1", "InstanceInitiatedShutdownBehavior": {"Value": "stop"}}
    assert fake.order[0] == "modify"  # 关机行为在打标签之前设置


@mock_aws
def test_protect_sets_both_attributes():
    ec2, infra = _setup()
    m = Ec2Manager(ec2, sleeper=lambda s: None)
    (iid,) = m.launch(1, "xb-p", 1, infra, "t3.nano")
    m.protect(iid)
    assert ec2.describe_instance_attribute(InstanceId=iid, Attribute="disableApiTermination")["DisableApiTermination"]["Value"] is True


def test_launch_requests_exact_batch_size():
    # 用假客户端断言下发给 RunInstances 的参数，不经过 moto 的 MinCount 行为
    class FakeEc2:
        def __init__(self):
            self.kw = None
        def run_instances(self, **kw):
            self.kw = kw
            return {"Instances": [{"InstanceId": f"i-{i}"} for i in range(kw["MaxCount"])]}
    fake = FakeEc2()
    m = Ec2Manager(fake, sleeper=lambda s: None)
    infra = Infra("subnet-x", "sg-x", "profile-x", "ami-x")
    ids = m.launch(3, "xb-run1", 2, infra, "t3.nano")
    assert fake.kw["MinCount"] == 3 and fake.kw["MaxCount"] == 3
    assert len(ids) == 3
