"""Read-only region, instance and public image discovery."""
import boto3


# Resolve publisher-maintained public parameters in the selected region; never
# reuse AMI IDs across regions or guess an architecture from the instance name.
IMAGE_PRESETS = (
    ("al2023", "Amazon Linux 2023", "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"),
    ("ubuntu2404", "Ubuntu 24.04 LTS", "/aws/service/canonical/ubuntu/server/24.04/stable/current/{ubuntu_arch}/hvm/ebs-gp3/ami-id"),
    ("ubuntu2204", "Ubuntu 22.04 LTS", "/aws/service/canonical/ubuntu/server/22.04/stable/current/{ubuntu_arch}/hvm/ebs-gp2/ami-id"),
)


def image_parameter(preset, arch):
    path = next(path for key, _, path in IMAGE_PRESETS if key == preset)
    return path.format(arch=arch, ubuntu_arch="amd64" if arch == "x86_64" else arch)


def type_spec(info):
    architectures = info.get("ProcessorInfo", {}).get("SupportedArchitectures", [])
    memory = info.get("MemoryInfo", {}).get("SizeInMiB")
    return {"type": info["InstanceType"], "vcpu": info.get("VCpuInfo", {}).get("DefaultVCpus"),
            "memory_gib": memory / 1024 if memory is not None else None,
            "architectures": architectures, "arch": "/".join(architectures)}


def image_catalog(ec2, ssm, instance_type, info=None):
    info = info or ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
    architectures = info["ProcessorInfo"]["SupportedArchitectures"]
    requests = [(key, label, arch, path.format(arch=arch, ubuntu_arch="amd64" if arch == "x86_64" else arch))
                for key, label, path in IMAGE_PRESETS for arch in architectures if arch in ("x86_64", "arm64")]
    if not requests:
        return {"images": [], "architectures": architectures, "notes": ["此机型暂无常用镜像，请手动输入兼容的 AMI ID。"]}
    response = ssm.get_parameters(Names=[p[3] for p in requests])
    parameters = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
    ids = sorted(set(parameters.values()))
    details = {image["ImageId"]: image for image in collect(ec2, "describe_images", "Images", ImageIds=ids)} if ids else {}
    images, notes = [], []
    for key, label, arch, path in requests:
        image = details.get(parameters.get(path))
        if (not image or image.get("State") != "available" or image.get("Architecture") != arch
                or image.get("RootDeviceType") != "ebs" or image.get("Platform") == "windows"):
            notes.append(f"{label}（{arch}）在当前区域暂无可用镜像。")
            continue
        root = next((m.get("Ebs", {}) for m in image.get("BlockDeviceMappings", [])
                     if m.get("DeviceName") == image.get("RootDeviceName")), {})
        images.append({"key": key, "name": label, "image_id": image["ImageId"], "architecture": arch,
                       "root_volume_size_gib": root.get("VolumeSize"), "source": path})
    return {"images": images, "architectures": architectures, "notes": notes}


def collect(client, operation, key, **kwargs):
    items = []
    while True:
        page = getattr(client, operation)(**kwargs)
        items.extend(page.get(key, []))
        if not page.get("NextToken"):
            return items
        kwargs["NextToken"] = page["NextToken"]


def region_catalog(ec2=None):
    session = boto3.Session()
    regions = {}
    for partition in session.get_available_partitions():
        for code in session.get_available_regions("ec2", partition_name=partition):
            regions[code] = {"code": code, "partition": partition, "status": "unknown"}
    error = ""
    if ec2 is not None:
        try:
            for region in ec2.describe_regions(AllRegions=True)["Regions"]:
                code = region["RegionName"]
                regions.setdefault(code, {"code": code, "partition": session.get_partition_for_region(code)})
                regions[code]["status"] = region.get("OptInStatus", "unknown")
        except Exception as exc:
            error = str(exc)
    return {"regions": sorted(regions.values(), key=lambda r: r["code"]), "error": error}


def instance_catalog(ec2, subnet_id="", include_details=True):
    location_type, filters, zone = "region", [], ""
    if subnet_id:
        subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        zone = subnet["AvailabilityZone"]
        location_type = "availability-zone"
        filters = [{"Name": "location", "Values": [zone]}]
    offerings = collect(ec2, "describe_instance_type_offerings", "InstanceTypeOfferings",
                        LocationType=location_type, Filters=filters)
    names = sorted({o["InstanceType"] for o in offerings})
    if not include_details:
        return {"availability_zone": zone, "instance_types": [type_spec({"InstanceType": name}) for name in names]}
    details = {}
    for start in range(0, len(names), 100):
        for item in collect(ec2, "describe_instance_types", "InstanceTypes",
                            InstanceTypes=names[start:start + 100]):
            details[item["InstanceType"]] = item
    return {"availability_zone": zone, "instance_types": [
        type_spec(details.get(name, {"InstanceType": name})) for name in names]}
