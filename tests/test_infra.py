import boto3
import pytest
from moto import mock_aws

from crossborder_selector.aws.infra import (
    ensure_infra, delete_infra, arch_for_instance_type, resolve_ami,
    SG_NAME, PING_SG_NAME, ROLE_NAME, PROFILE_NAME, AMI_PARAMS,
)
from crossborder_selector.config import load_config

REGION = "us-east-1"


def _clients():
    return (boto3.client("ec2", region_name=REGION), boto3.client("iam", region_name=REGION),
            boto3.client("ssm", region_name=REGION))


def _seed_ami(ssm, ec2, arch="x86_64"):
    # moto 5 预置了 /aws/service/ 保留公共参数且禁止写入，故直接读取其默认 AMI 值
    return ssm.get_parameters(Names=[AMI_PARAMS[arch]])["Parameters"][0]["Value"]


def test_arch_detection():
    assert arch_for_instance_type("t3.nano") == "x86_64"
    assert arch_for_instance_type("t4g.nano") == "arm64"
    assert arch_for_instance_type("c6gn.large") == "arm64"
    assert arch_for_instance_type("g4dn.xlarge") == "x86_64"


@mock_aws
def test_ensure_infra_creates_then_reuses():
    ec2, iam, ssm = _clients()
    ami = _seed_ami(ssm, ec2)
    cfg = load_config(None, {"region": REGION})
    a = ensure_infra(ec2, iam, ssm, cfg)
    b = ensure_infra(ec2, iam, ssm, cfg)
    assert a == b
    assert a.image_id == ami and a.instance_profile_name == PROFILE_NAME
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [PING_SG_NAME]}])["SecurityGroups"]
    assert len(sgs) == 1
    assert sgs[0]["IpPermissions"][0]["IpProtocol"] == "icmp"
    assert sgs[0]["IpPermissions"][0]["FromPort"] == 8
    assert {t["Key"]: t["Value"] for t in sgs[0]["Tags"]}["crossborder-managed"] == "true"
    roles = iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]
    assert any(p["PolicyName"] == "AmazonSSMManagedInstanceCore" for p in roles)


@mock_aws
def test_ensure_infra_honours_explicit_ids():
    ec2, iam, ssm = _clients()
    image = _seed_ami(ssm, ec2)
    vpc = ec2.create_vpc(CidrBlock="10.9.0.0/16")["Vpc"]["VpcId"]
    # 固定到提供 t3.nano 的可用区：moto 随机分配的 us-east-1e 不在该机型的 offerings 里
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.9.1.0/24", AvailabilityZone=f"{REGION}a")["Subnet"]["SubnetId"]
    sg = ec2.create_security_group(GroupName="mine", Description="d", VpcId=vpc)["GroupId"]
    cfg = load_config(None, {"region": REGION, "subnet_id": subnet, "security_group_id": sg,
                             "image_id": image, "instance_profile_name": "my-profile",
                             "disable_backends": ["globalping"]})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    assert infra.subnet_id == subnet and infra.security_group_id == sg
    assert infra.image_id == image and infra.instance_profile_name == "my-profile"


def test_missing_default_vpc_raises():
    class NoDefaultVpc:
        class meta:
            region_name = REGION
        def describe_vpcs(self, Filters):
            return {"Vpcs": []}
    from crossborder_selector.aws.infra import find_default_subnet
    with pytest.raises(RuntimeError, match="no default VPC"):
        find_default_subnet(NoDefaultVpc())


@mock_aws
def test_delete_infra_removes_managed_regional_groups_and_keeps_iam():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    ensure_infra(ec2, iam, ssm, load_config(None, {"region": REGION}))
    delete_infra(ec2, iam)
    assert ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME, PING_SG_NAME]}])["SecurityGroups"] == []
    assert iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]


@mock_aws
def test_delete_infra_skips_unmanaged_same_name_sg():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    # 另建一个同名但无 crossborder-managed 标签的 SG（模拟他人创建），delete_infra 不应删它
    vpc = ec2.create_vpc(CidrBlock="10.7.0.0/16")["Vpc"]["VpcId"]
    other = ec2.create_security_group(GroupName=SG_NAME, Description="not ours", VpcId=vpc)["GroupId"]
    delete_infra(ec2, iam)
    remaining = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]
    assert [s["GroupId"] for s in remaining] == [other]


def test_delete_infra_dependency_violation_raises_runtimeerror():
    from botocore.exceptions import ClientError

    class FakeEc2:
        def describe_security_groups(self, Filters):
            return {"SecurityGroups": [{"GroupId": "sg-1"}]}
        def delete_security_group(self, GroupId):
            raise ClientError({"Error": {"Code": "DependencyViolation", "Message": "in use"}}, "DeleteSecurityGroup")
    with pytest.raises(RuntimeError, match="still in use"):
        delete_infra(FakeEc2(), object())


@mock_aws
def test_resolve_ami_arm():
    ec2, iam, ssm = _clients()
    ami = _seed_ami(ssm, ec2, "arm64")
    assert resolve_ami(ssm, "t4g.nano") == ami


@mock_aws
def test_ensure_infra_records_alternate_subnets_in_other_azs():
    """默认 VPC 下记录同 VPC、其他可用区且提供该机型的子网，供容量不足时回退。"""
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    cfg = load_config(None, {"region": REGION})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    alts = infra.alternate_subnets[cfg.instance_type]
    assert infra.subnet_id not in alts and len(alts) >= 1
    zones = {s["AvailabilityZone"] for s in ec2.describe_subnets(SubnetIds=[infra.subnet_id, *alts])["Subnets"]}
    assert len(zones) == 1 + len(alts)  # 每个备选子网在不同可用区
    vpcs = {s["VpcId"] for s in ec2.describe_subnets(SubnetIds=[infra.subnet_id, *alts])["Subnets"]}
    assert len(vpcs) == 1


@mock_aws
def test_explicit_subnet_has_no_alternates():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    # 选一个确实提供该机型的子网：直接复用自动选择的结果
    subnet = ensure_infra(ec2, iam, ssm, load_config(None, {"region": REGION})).subnet_id
    cfg = load_config(None, {"region": REGION, "subnet_id": subnet})
    infra = ensure_infra(ec2, iam, ssm, cfg)
    assert infra.subnet_id == subnet and infra.alternate_subnets.get(cfg.instance_type, []) == []


# ---- 每次运行独立的拨测安全组 ----

def _default_vpc(ec2):
    return ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]


@mock_aws
def test_probe_security_group_opens_only_configured_tcp_ports_from_given_sources():
    from crossborder_selector.aws.infra import ensure_probe_security_group, delete_probe_security_group, probe_sg_name, RUN_TAG_KEY
    ec2, _, _ = _clients()
    vpc = _default_vpc(ec2)
    gid = ensure_probe_security_group(ec2, vpc, "xb-p1", [443, 8443], ["203.0.113.7/32", "198.51.100.0/24"])
    g = ec2.describe_security_groups(GroupIds=[gid])["SecurityGroups"][0]
    assert g["GroupName"] == probe_sg_name("xb-p1") == "crossborder-probe-xb-p1"
    tags = {t["Key"]: t["Value"] for t in g["Tags"]}
    assert tags["crossborder-managed"] == "true" and tags[RUN_TAG_KEY] == "xb-p1"
    rules = {(r["IpProtocol"], r["FromPort"], r["ToPort"], tuple(sorted(i["CidrIp"] for i in r["IpRanges"])))
             for r in g["IpPermissions"]}
    assert rules == {("tcp", 443, 443, ("198.51.100.0/24", "203.0.113.7/32")),
                     ("tcp", 8443, 8443, ("198.51.100.0/24", "203.0.113.7/32"))}
    assert ensure_probe_security_group(ec2, vpc, "xb-p1", [443, 8443], ["203.0.113.7/32", "198.51.100.0/24"]) == gid  # 幂等
    assert delete_probe_security_group(ec2, "xb-p1") is True
    assert not ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [probe_sg_name("xb-p1")]}])["SecurityGroups"]
    assert delete_probe_security_group(ec2, "xb-p1") is False  # 不存在时返回 False，不抛


@mock_aws
def test_probe_security_group_without_sources_is_not_created():
    from crossborder_selector.aws.infra import ensure_probe_security_group
    ec2, _, _ = _clients()
    assert ensure_probe_security_group(ec2, _default_vpc(ec2), "xb-p2", [443], []) == ""


@mock_aws
def test_ensure_infra_with_probe_sources_sets_probe_group_and_listener_user_data():
    ec2, iam, ssm = _clients()
    _seed_ami(ssm, ec2)
    cfg = load_config(None, {"region": REGION, "backends": {"agent": {"enabled": True, "transport": "http",
                                                                        "tcp_ports": [443, 22]}}})
    infra = ensure_infra(ec2, iam, ssm, cfg, run_id="xb-p3", probe_source_cidrs=["203.0.113.7/32"])
    assert infra.probe_security_group_id.startswith("sg-") and infra.probe_security_group_id != infra.security_group_id
    assert "443" in infra.user_data and "22" in infra.user_data and "crossborder_listener" in infra.user_data
    # 未提供来源：不建拨测组、不注入监听
    plain = ensure_infra(ec2, iam, ssm, cfg, run_id="xb-p4", probe_source_cidrs=[])
    assert plain.probe_security_group_id == "" and plain.user_data == ""
    # 未启用 agent：即使给了来源也不开 TCP
    cfg2 = load_config(None, {"region": REGION})
    assert ensure_infra(ec2, iam, ssm, cfg2, run_id="xb-p5", probe_source_cidrs=["203.0.113.7/32"]).probe_security_group_id == ""
