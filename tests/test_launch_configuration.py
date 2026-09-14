import pytest
from botocore.exceptions import ClientError

from crossborder_selector.aws.catalog import instance_catalog, region_catalog
from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import Infra, prepare_launch
from crossborder_selector.config import load_config
from crossborder_selector.reputation.badlist import BadListSource
from crossborder_selector.reputation.base import score_reputation
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import _DemoEc2, _DemoSsm
from tests.test_web_api import _factory, _err
from crossborder_selector.aws.infra import find_default_subnet


def test_catalog_reads_all_pages_and_batches_metadata_without_a_fixed_allowlist():
    class EC2(_DemoEc2):
        def __init__(self):
            self.requests = []
        def describe_instance_type_offerings(self, **kwargs):
            assert kwargs["LocationType"] == "availability-zone"
            assert kwargs["Filters"] == [{"Name": "location", "Values": ["ap-east-1a"]}]
            if kwargs.get("NextToken"):
                return {"InstanceTypeOfferings": [{"InstanceType": "m99i.large"}]}
            return {"InstanceTypeOfferings": [{"InstanceType": f"m{i}i.large"} for i in range(1, 102)],
                    "NextToken": "page2"}
        def describe_instance_types(self, InstanceTypes, **kwargs):
            self.requests.append(InstanceTypes)
            assert len(InstanceTypes) <= 100
            return super().describe_instance_types(InstanceTypes)
    ec2 = EC2()
    result = instance_catalog(ec2, "subnet-demo")
    assert len(result["instance_types"]) == 101
    assert "m99i.large" in {t["type"] for t in result["instance_types"]}
    assert len(ec2.requests) == 2


def test_region_catalog_keeps_new_aws_regions_and_sdk_partitions():
    class EC2:
        def describe_regions(self, AllRegions):
            assert AllRegions is True
            return {"Regions": [{"RegionName": "ap-example-9", "OptInStatus": "not-opted-in"}]}
    regions = {r["code"]: r for r in region_catalog(EC2())["regions"]}
    assert {"us-east-1", "cn-north-1", "us-gov-west-1", "ap-example-9"} <= set(regions)
    assert regions["ap-example-9"]["status"] == "not-opted-in"
    assert load_config(overrides={"region": "ap-example-9"}).region == "ap-example-9"


def test_default_subnet_is_selected_from_zones_offering_the_chosen_type():
    class EC2(_DemoEc2):
        def describe_instance_type_offerings(self, **kwargs):
            return {"InstanceTypeOfferings": [{"InstanceType": "m9i.large", "Location": "ap-east-1b"}]}
    assert find_default_subnet(EC2(), instance_type="m9i.large")[1] == "subnet-demo-b"


@pytest.mark.parametrize("bad", [
    {"root_volume_size_gib": 0}, {"root_volume_size_gib": 1.5},
    {"root_volume_type": "st1"}, {"root_volume_encrypted": "false"},
    {"batch_size": 1, "instance_overrides": [{}, {}]},
    {"instance_overrides": [{"instance_type": "c7i.large"}]},
    {"instance_overrides": [{"root_volume_size_gib": -1}]},
])
def test_invalid_storage_configuration_is_rejected(bad):
    with pytest.raises(ValueError):
        load_config(overrides=bad)


def test_per_instance_images_and_root_device_names_are_applied_to_ec2_requests():
    class EC2(_DemoEc2):
        def describe_images(self, ImageIds):
            images = super().describe_images(ImageIds)["Images"]
            for image in images:
                if image["ImageId"] == "ami-custom":
                    image["RootDeviceName"] = "/dev/sda1"
                    image["BlockDeviceMappings"][0]["DeviceName"] = "/dev/sda1"
            return {"Images": images}
        def run_instances(self, **kwargs):
            self.calls.append(kwargs)
            return {"Instances": [{"InstanceId": f"i-{len(self.calls)}-{i}"} for i in range(kwargs["MaxCount"])]}
    ec2 = EC2()
    cfg = load_config(overrides={"batch_size": 3, "root_volume_size_gib": 20, "instance_overrides": [
        {}, {"image_id": "ami-custom", "root_volume_size_gib": 40, "root_volume_type": "gp2"}, {}]})
    templates = prepare_launch(ec2, _DemoSsm(), cfg)
    infra = Infra("subnet", "sg", "profile", templates[0]["ImageId"], templates)
    ids = Ec2Manager(ec2).launch(3, "xb-test", 1, infra, cfg.instance_type)
    assert len(ids) == 3 and len(ec2.calls) == 2
    default, custom = ec2.calls
    assert default["MinCount"] == default["MaxCount"] == 2
    assert default["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] == 20
    assert custom["ImageId"] == "ami-custom"
    assert custom["BlockDeviceMappings"] == [{"DeviceName": "/dev/sda1", "Ebs": {
        "VolumeSize": 40, "VolumeType": "gp2", "Encrypted": True, "DeleteOnTermination": True}}]


def test_invalid_image_architecture_and_small_disk_fail_before_launch():
    with pytest.raises(ValueError, match="架构"):
        prepare_launch(_DemoEc2(), _DemoSsm(), load_config(overrides={"image_id": "ami-arm64"}))
    with pytest.raises(ValueError, match="根卷"):
        prepare_launch(_DemoEc2(), _DemoSsm(), load_config(overrides={"root_volume_size_gib": 1}))


def test_failed_later_launch_group_cleans_up_already_started_instances():
    class EC2:
        def __init__(self):
            self.count = 0
            self.terminated = []
        def run_instances(self, **kwargs):
            self.count += 1
            if self.count == 2:
                raise _err("InsufficientInstanceCapacity")
            return {"Instances": [{"InstanceId": "i-started"}]}
        def terminate_instances(self, InstanceIds):
            self.terminated.extend(InstanceIds)
    ec2 = EC2()
    infra = Infra("s", "g", "p", "ami-a", ({"ImageId": "ami-a"}, {"ImageId": "ami-b"}))
    with pytest.raises(ClientError):
        Ec2Manager(ec2).launch(2, "xb-test", 1, infra, "t3.nano")
    assert ec2.terminated == ["i-started"]


def test_capacity_uses_dynamic_type_vcpus_and_separate_gpu_quota(tmp_path):
    clients = _factory()(None)
    class EC2(_DemoEc2):
        def describe_instances(self, **kwargs):
            return {"Reservations": [{"Instances": [
                {"InstanceType": "g9.xlarge"}, {"InstanceType": "t3.nano"},
                {"InstanceType": "g9.xlarge", "InstanceLifecycle": "spot"}]}]}
    class Quotas:
        def list_service_quotas(self, ServiceCode):
            return {"Quotas": [{"QuotaName": "Running On-Demand G and VT instances", "Value": 8}]}
    clients.update(ec2=EC2(), **{"service-quotas": Quotas()})
    api = Api(factory=lambda cfg: clients, cwd=str(tmp_path))
    capacity = api.capacity(load_config(overrides={"instance_type": "g9.xlarge", "batch_size": 2, "max_rounds": 2}))
    assert capacity["used_vcpus"] == 2
    assert capacity["max_launch_count"] == 2
    assert not capacity["problems"]


def test_badlist_matches_cidr_and_preserves_source_for_non_matches():
    source = BadListSource("https://example.invalid/list.txt", fetcher=lambda _: "# source\n192.0.2.0/24\n198.51.100.2\t5\n")
    assert source.check("192.0.2.42").listed
    assert source.check("198.51.100.2").listed
    result = source.check("203.0.113.1")
    assert not result.listed and "https://example.invalid/list.txt" in result.detail


@pytest.mark.parametrize("text", ["", "# only comments", "<html>GitHub repository</html>"])
def test_empty_or_html_badlist_is_unknown_and_fetched_once(text):
    calls = []
    def fetch(url):
        calls.append(url)
        return text
    source = BadListSource("https://example.invalid/list", fetcher=fetch)
    for address in ("192.0.2.1", "192.0.2.2"):
        result = score_reputation(address, [source])
        assert result.status == "unknown" and result.score is None
    assert len(calls) == 1


def test_badlist_failure_cannot_become_a_retained_instance_by_default():
    from crossborder_selector.orchestrator import Orchestrator
    from tests.test_orchestrator import FakeEc2, FakeSsm, ScriptedBackend, INFRA
    ec2 = FakeEc2(["192.0.2.1"])
    source = BadListSource("https://example.invalid/list", fetcher=lambda _: "<html>Unavailable</html>")
    cfg = load_config(overrides={"batch_size": 1, "max_rounds": 1})
    result = Orchestrator(cfg, ec2, FakeSsm(), [ScriptedBackend({})], [source],
                          lambda _: "", INFRA, "xb-test", log=lambda _: None).run()
    assert not result.winners and ec2.terminated == ["i-1"]
    assert result.rounds[0].vetoed[0].veto_reason == "reputation_unavailable"
