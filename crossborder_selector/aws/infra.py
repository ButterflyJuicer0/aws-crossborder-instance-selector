"""跨 run 复用的基础设施：默认子网、无入站 SG、SSM instance profile、AL2023 AMI。全部幂等。"""
import json
import re
from dataclasses import dataclass

from botocore.exceptions import ClientError

SG_NAME = "crossborder-selector-sg"
ROLE_NAME = "crossborder-selector-ssm-role"
PROFILE_NAME = "crossborder-selector-ssm"
MANAGED_TAG = {"Key": "crossborder-managed", "Value": "true"}
SSM_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
AMI_PARAMS = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
}
_TRUST = json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]})


@dataclass(frozen=True)
class Infra:
    subnet_id: str
    security_group_id: str
    instance_profile_name: str
    image_id: str


def arch_for_instance_type(instance_type: str) -> str:
    family = instance_type.split(".")[0]
    return "arm64" if re.match(r"^[a-z]+\d+[a-z]*g[a-z]*$", family) else "x86_64"


def resolve_ami(ssm, instance_type: str) -> str:
    r = ssm.get_parameters(Names=[AMI_PARAMS[arch_for_instance_type(instance_type)]])
    if not r["Parameters"]:
        raise RuntimeError("cannot resolve AL2023 AMI via SSM public parameter")
    return r["Parameters"][0]["Value"]


def find_default_subnet(ec2, subnet_id: str = ""):
    """返回 (vpc_id, subnet_id)。给了 subnet_id 就只查它的 VPC。"""
    if subnet_id:
        s = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        return s["VpcId"], subnet_id
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError(f"no default VPC in region {ec2.meta.region_name}; "
                           "set subnet_id and security_group_id in config")
    vpc = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"default VPC {vpc} has no subnet; set subnet_id in config")
    return vpc, sorted(subnets, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]


def ensure_security_group(ec2, vpc_id: str) -> str:
    """按名字查找；不存在则创建。无任何入站规则。"""
    found = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [SG_NAME]}, {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    return ec2.create_security_group(
        GroupName=SG_NAME, Description="crossborder selector probe egress-only", VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": [MANAGED_TAG]}])["GroupId"]


def ensure_instance_profile(iam) -> str:
    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        return PROFILE_NAME
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    try:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=_TRUST,
                        Description="crossborder selector SSM role", Tags=[MANAGED_TAG])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=SSM_POLICY_ARN)
    iam.create_instance_profile(InstanceProfileName=PROFILE_NAME, Tags=[MANAGED_TAG])
    iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    return PROFILE_NAME


def ensure_infra(ec2, iam, ssm, cfg) -> Infra:
    vpc_id, subnet_id = find_default_subnet(ec2, cfg.subnet_id)
    sg = cfg.security_group_id or ensure_security_group(ec2, vpc_id)
    profile = cfg.instance_profile_name or ensure_instance_profile(iam)
    image = cfg.image_id or resolve_ami(ssm, cfg.instance_type)
    return Infra(subnet_id, sg, profile, image)


def delete_infra(ec2, iam) -> None:
    """删除工具自建的 SG 与 IAM 资源。调用方负责先确认没有 winner 依赖。"""
    for sg in ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NAME]}])["SecurityGroups"]:
        ec2.delete_security_group(GroupId=sg["GroupId"])
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    except ClientError:
        pass
    for fn, kw in ((iam.delete_instance_profile, {"InstanceProfileName": PROFILE_NAME}),
                   (iam.detach_role_policy, {"RoleName": ROLE_NAME, "PolicyArn": SSM_POLICY_ARN}),
                   (iam.delete_role, {"RoleName": ROLE_NAME})):
        try:
            fn(**kw)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
