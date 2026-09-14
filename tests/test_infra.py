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
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.9.1.0/24")["Subnet"]["SubnetId"]
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
