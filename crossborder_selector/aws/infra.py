"""可复用的子网、安全组、SSM 实例配置及 AL2023 镜像。"""
import json
import re
from dataclasses import dataclass

from botocore.exceptions import ClientError
from crossborder_selector.config import enabled_backends, launch_specs, launch_groups
from crossborder_selector.aws.catalog import collect, image_parameter

SG_NAME = "crossborder-selector-sg"
PING_SG_NAME = "crossborder-selector-ping-sg"
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
    launch_templates: tuple = ()


def arch_for_instance_type(instance_type: str) -> str:
    family = instance_type.split(".")[0]
    return "arm64" if re.match(r"^[a-z]+\d+[a-z]*g[a-z]*$", family) else "x86_64"


def resolve_ami(ssm, instance_type: str) -> str:
    r = ssm.get_parameters(Names=[AMI_PARAMS[arch_for_instance_type(instance_type)]])
    if not r["Parameters"]:
        raise RuntimeError("cannot resolve AL2023 AMI via SSM public parameter")
    return r["Parameters"][0]["Value"]


def prepare_launch(ec2, ssm, cfg):
    """Validate every requested image and disk before creating any infrastructure."""
    names = sorted({g["instance_type"] for g in launch_groups(cfg)})
    infos = {i["InstanceType"]: i for i in collect(ec2, "describe_instance_types", "InstanceTypes", InstanceTypes=names)}
    images, templates = {}, []
    automatic = {}
    for index, spec in enumerate(launch_specs(cfg)):
        name = spec["instance_type"]
        architectures = infos[name]["ProcessorInfo"]["SupportedArchitectures"]
        image_id = spec["image_id"]
        if not image_id:
            arch = next((a for a in architectures if a in AMI_PARAMS), None)
            if arch is None:
                raise ValueError(f"{name} 没有自动镜像，请指定兼容的 AMI ID")
            parameter = image_parameter(spec["image_preset"] or "al2023", arch)
            if parameter not in automatic:
                response = ssm.get_parameters(Names=[parameter])
                if not response.get("Parameters"):
                    raise ValueError(f"无法获取 {name} 的 {spec['image_preset'] or 'al2023'} 镜像，请指定 AMI ID")
                automatic[parameter] = response["Parameters"][0]["Value"]
            image_id = automatic[parameter]
        if image_id not in images:
            found = ec2.describe_images(ImageIds=[image_id])["Images"]
            if not found:
                raise ValueError(f"镜像 {image_id} 在 {cfg.region} 不存在或无权访问")
            images[image_id] = found[0]
        image = images[image_id]
        if image.get("State") != "available":
            raise ValueError(f"镜像 {image_id} 尚不可用")
        if image.get("Architecture") not in architectures:
            raise ValueError(f"第 {index + 1} 台镜像 {image_id} 的架构与 {name} 不兼容")
        if cfg.backends["reverse"]["enabled"] and image.get("Platform") == "windows":
            raise ValueError("反向探测使用 Linux 脚本；Windows AMI 请关闭反向探测并启用外部探测")
        root = image.get("RootDeviceName")
        mapping = next((m for m in image.get("BlockDeviceMappings", []) if m.get("DeviceName") == root), {})
        if image.get("RootDeviceType") != "ebs" or "Ebs" not in mapping:
            raise ValueError(f"镜像 {image_id} 需要 EBS 根卷")
        minimum = mapping["Ebs"].get("VolumeSize")
        if not minimum:
            snapshot = mapping["Ebs"].get("SnapshotId")
            if not snapshot:
                raise ValueError(f"无法确认镜像 {image_id} 的根卷最小容量")
            minimum = ec2.describe_snapshots(SnapshotIds=[snapshot])["Snapshots"][0]["VolumeSize"]
        size = spec["root_volume_size_gib"] or minimum
        maximum = {"gp3": 65536, "gp2": 16384, "standard": 1024}[spec["root_volume_type"]]
        if not minimum <= size <= maximum:
            raise ValueError(f"第 {index + 1} 台根卷需为 {minimum}–{maximum} GiB")
        if mapping["Ebs"].get("Encrypted") and not spec["root_volume_encrypted"]:
            raise ValueError(f"镜像 {image_id} 使用加密快照，不能创建未加密根卷")
        templates.append({"InstanceType": name, "ImageId": image_id, "BlockDeviceMappings": [{
            "DeviceName": root, "Ebs": {"VolumeSize": size, "VolumeType": spec["root_volume_type"],
                                      "Encrypted": spec["root_volume_encrypted"], "DeleteOnTermination": True}}]})
    return tuple(templates)


def prepare_subnets(ec2, cfg):
    """Choose a supported subnet per type, inside one VPC for the shared SG."""
    result, vpc = {}, None
    for group in launch_groups(cfg):
        name = group["instance_type"]
        if name in result:
            continue
        selected_vpc, subnet = find_default_subnet(ec2, cfg.subnet_id, name)
        if vpc is not None and vpc != selected_vpc:
            raise ValueError("所有候选子网必须属于同一个 VPC")
        vpc = selected_vpc
        if cfg.subnet_id:
            zone = ec2.describe_subnets(SubnetIds=[subnet])["Subnets"][0]["AvailabilityZone"]
            offers = collect(ec2, "describe_instance_type_offerings", "InstanceTypeOfferings",
                             LocationType="availability-zone", Filters=[
                                 {"Name": "location", "Values": [zone]}, {"Name": "instance-type", "Values": [name]}])
            if not any(o["InstanceType"] == name for o in offers):
                raise ValueError(f"{zone} 不提供 {name}，请选择其他机型或子网")
        result[name] = subnet
    return vpc, result


def find_default_subnet(ec2, subnet_id: str = "", instance_type: str = ""):
    """返回 (vpc_id, subnet_id)。给了 subnet_id 就只查它的 VPC。"""
    if subnet_id:
        s = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        return s["VpcId"], subnet_id
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError(f"no default VPC in region {ec2.meta.region_name}; "
                           "set subnet_id in config (security_group_id is optional)")
    vpc = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"default VPC {vpc} has no subnet; set subnet_id in config")
    if instance_type:
        offerings = collect(ec2, "describe_instance_type_offerings", "InstanceTypeOfferings",
                            LocationType="availability-zone",
                            Filters=[{"Name": "instance-type", "Values": [instance_type]}])
        zones = {item["Location"] for item in offerings}
        subnets = [s for s in subnets if s["AvailabilityZone"] in zones]
        if not subnets:
            raise RuntimeError(f"默认 VPC 的子网所在可用区不提供 {instance_type}，请指定其他子网")
    return vpc, sorted(subnets, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]


def needs_inbound_ping(cfg):
    return any(name != "reverse" for name in enabled_backends(cfg.backends))


def security_group_name(cfg):
    return PING_SG_NAME if needs_inbound_ping(cfg) else SG_NAME


def _allows_public_ping(group):
    return any(
        rule.get("IpProtocol") == "-1" or
        (rule.get("IpProtocol") in ("icmp", "1") and rule.get("FromPort") in (-1, 8)
         and rule.get("ToPort") in (-1, 0))
        for rule in group.get("IpPermissions", [])
        if any(ip.get("CidrIp") == "0.0.0.0/0" for ip in rule.get("IpRanges", [])))


def validate_security_group(ec2, group_id, vpc_id, public_ping):
    group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    if group["VpcId"] != vpc_id:
        raise RuntimeError("security group and subnet must belong to the same VPC")
    if public_ping and not _allows_public_ping(group):
        raise RuntimeError("external ping probes require inbound IPv4 ICMP Echo Request (type 8, code 0) "
                           "from 0.0.0.0/0; update your security group or disable external probes")


def ensure_security_group(ec2, vpc_id: str, public_ping=False) -> str:
    """外部探测使用独立受管组；只允许 ICMP Echo Request，不开放 TCP/UDP。"""
    name = PING_SG_NAME if public_ping else SG_NAME
    found = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    if found:
        group = found[0]
        group_id = group["GroupId"]
        if not public_ping or _allows_public_ping(group):
            return group_id
        if MANAGED_TAG not in group.get("Tags", []):
            raise RuntimeError(f"refusing to modify unmanaged security group {name}")
    else:
        group_id = ec2.create_security_group(
            GroupName=name, Description="crossborder selector ICMP probe" if public_ping else "crossborder selector egress-only",
            VpcId=vpc_id, TagSpecifications=[{"ResourceType": "security-group", "Tags": [MANAGED_TAG]}])["GroupId"]
    if public_ping:
        try:
            ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[{
                "IpProtocol": "icmp", "FromPort": 8, "ToPort": 0,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Public ping measurements"}]}])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
    return group_id


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
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=SSM_POLICY_ARN.replace("arn:aws:", f"arn:{iam.meta.partition}:"))
    iam.create_instance_profile(InstanceProfileName=PROFILE_NAME, Tags=[MANAGED_TAG])
    iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
    return PROFILE_NAME


def ensure_infra(ec2, iam, ssm, cfg) -> Infra:
    templates = prepare_launch(ec2, ssm, cfg)
    vpc_id, subnets = prepare_subnets(ec2, cfg)
    templates = tuple({**t, "SubnetId": subnets[t["InstanceType"]]} for t in templates)
    sg = cfg.security_group_id or ensure_security_group(ec2, vpc_id, needs_inbound_ping(cfg))
    validate_security_group(ec2, sg, vpc_id, needs_inbound_ping(cfg))
    profile = cfg.instance_profile_name or ensure_instance_profile(iam)
    return Infra(subnets[cfg.instance_type], sg, profile, templates[0]["ImageId"], templates)


def delete_infra(ec2, iam) -> None:
    """仅删除当前区域的受管安全组。账户级 IAM 资源单独检查、删除。"""
    # 只删本工具打了 crossborder-managed=true 标签的 SG，避免误删同名的他人安全组
    sgs = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [SG_NAME, PING_SG_NAME]},
        {"Name": "tag:crossborder-managed", "Values": ["true"]}])["SecurityGroups"]
    for sg in sgs:
        try:
            ec2.delete_security_group(GroupId=sg["GroupId"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "DependencyViolation":
                raise RuntimeError("security group still in use by shutting-down instances; retry in a minute")
            raise


def delete_shared_iam(ec2, iam, region_client_factory):
    """核验所有权及所有已启用区域的依赖后，删除账户级共享 IAM 资源。"""
    resources = {}
    for kind, method, key, name in (
            ("InstanceProfile", iam.get_instance_profile, "InstanceProfileName", PROFILE_NAME),
            ("Role", iam.get_role, "RoleName", ROLE_NAME)):
        try:
            item = method(**{key: name})[kind]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            continue
        if MANAGED_TAG not in item.get("Tags", []):
            raise RuntimeError(f"refusing to delete unmanaged IAM {kind}: {name}")
        resources[kind] = item
    if not resources:
        return
    profile = resources.get("InstanceProfile")
    if profile and any(r["RoleName"] != ROLE_NAME for r in profile.get("Roles", [])):
        raise RuntimeError("shared instance profile contains an unexpected role")
    if "Role" in resources:
        profiles = iam.get_paginator("list_instance_profiles_for_role").paginate(RoleName=ROLE_NAME)
        if any(p["InstanceProfileName"] != PROFILE_NAME for page in profiles for p in page["InstanceProfiles"]):
            raise RuntimeError("shared IAM role is used by another instance profile")
    if profile:
        regions = ec2.describe_regions(AllRegions=False)["Regions"]
        if not regions:
            raise RuntimeError("cannot verify IAM dependencies: no enabled regions returned")
        for region in regions:
            client = region_client_factory(region["RegionName"])
            pages = client.get_paginator("describe_instances").paginate(Filters=[
                {"Name": "iam-instance-profile.arn", "Values": [profile["Arn"]]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}])
            if any(res["Instances"] for page in pages for res in page["Reservations"]):
                raise RuntimeError(f"shared IAM instance profile is still used in {region['RegionName']}")
    # No mutation before every dependency query above has succeeded.
    if profile:
        if profile.get("Roles"):
            iam.remove_role_from_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
        iam.delete_instance_profile(InstanceProfileName=PROFILE_NAME)
    if "Role" in resources:
        iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=SSM_POLICY_ARN.replace("arn:aws:", f"arn:{iam.meta.partition}:"))
        iam.delete_role(RoleName=ROLE_NAME)
