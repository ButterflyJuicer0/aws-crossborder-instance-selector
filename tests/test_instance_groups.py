from collections import Counter

import pytest
from botocore.exceptions import ClientError

from crossborder_selector.aws.ec2 import Ec2Manager
from crossborder_selector.aws.infra import Infra, prepare_launch, prepare_subnets
from crossborder_selector.config import load_config, launch_specs
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import _DemoEc2, _DemoSsm
from tests.test_web_api import _factory, _err


GROUPS = [{"instance_type": "t3.nano", "count": 2}, {"instance_type": "t4g.nano", "count": 3}]


def test_groups_derive_total_and_preserve_legacy_configuration():
    cfg = load_config(overrides={"instance_groups": GROUPS, "batch_size": 49})
    assert cfg.batch_size == 5
    assert Counter(s["instance_type"] for s in launch_specs(cfg)) == {"t3.nano": 2, "t4g.nano": 3}
    assert len(launch_specs(load_config(overrides={"batch_size": 3}))) == 3


@pytest.mark.parametrize("groups", [
    None, {}, [{"instance_type": "bad", "count": 1}],
    [{"instance_type": "t3.nano", "count": True}],
    [{"instance_type": "t3.nano", "count": 1.5}],
    [{"instance_type": "t3.nano", "count": 0}],
    [{"instance_type": "t3.nano", "count": 30}, {"instance_type": "t4g.nano", "count": 21}],
    [{"instance_type": "t3.nano", "count": 2, "typo": 1}],
])
def test_invalid_groups_are_rejected(groups):
    with pytest.raises(ValueError):
        load_config(overrides={"instance_groups": groups})


def test_mixed_architecture_groups_launch_exact_counts_and_images():
    class EC2(_DemoEc2):
        def run_instances(self, **kwargs):
            self.calls.append(kwargs)
            return {"Instances": [{"InstanceId": f"i-{len(self.calls)}-{n}"} for n in range(kwargs["MaxCount"])]}
    ec2 = EC2()
    cfg = load_config(overrides={"instance_groups": GROUPS, "image_preset": "ubuntu2404",
                                "instance_overrides": [{}, {}, {"image_preset": "ubuntu2204", "root_volume_size_gib": 20}]})
    templates = prepare_launch(ec2, _DemoSsm(), cfg)
    assert "x86_64" in templates[0]["ImageId"]
    assert "ubuntu2204-arm64" in templates[2]["ImageId"]
    assert "ubuntu2404-arm64" in templates[3]["ImageId"]
    manager = Ec2Manager(ec2)
    assert len(manager.launch(5, "xb-mixed", 1, Infra("s", "g", "p", "", templates), cfg.instance_type)) == 5
    counts = Counter()
    for request in ec2.calls:
        assert request["MinCount"] == request["MaxCount"]
        counts[request["InstanceType"]] += request["MaxCount"]
    assert counts == {"t3.nano": 2, "t4g.nano": 3}
    assert {s["InstanceType"] for s in manager.launch_settings.values()} == {"t3.nano", "t4g.nano"}


def test_incompatible_manual_image_fails_before_any_launch():
    cfg = load_config(overrides={"instance_groups": GROUPS, "image_id": "ami-demo-x86_64"})
    with pytest.raises(ValueError, match="t4g.nano"):
        prepare_launch(_DemoEc2(), _DemoSsm(), cfg)


def test_default_subnets_can_differ_by_type_but_explicit_subnet_must_offer_every_type():
    class EC2(_DemoEc2):
        def describe_instance_type_offerings(self, **kwargs):
            filters = {f["Name"]: f["Values"] for f in kwargs["Filters"]}
            name = filters["instance-type"][0]
            zone = "ap-east-1a" if name == "t3.nano" else "ap-east-1b"
            if "location" in filters and zone not in filters["location"]:
                return {"InstanceTypeOfferings": []}
            return {"InstanceTypeOfferings": [{"InstanceType": name, "Location": zone}]}
    cfg = load_config(overrides={"instance_groups": GROUPS})
    vpc, subnets = prepare_subnets(EC2(), cfg)
    assert vpc == "vpc-demo" and subnets == {"t3.nano": "subnet-demo-a", "t4g.nano": "subnet-demo-b"}
    cfg.subnet_id = "subnet-demo-a"
    with pytest.raises(ValueError, match="不提供 t4g.nano"):
        prepare_subnets(EC2(), cfg)


def test_shared_quota_adds_all_types_and_reserves_largest_possible_winners(tmp_path):
    clients = _factory(quotas={"value": 12})(None)
    cfg = load_config(overrides={"instance_groups": GROUPS, "max_rounds": 2, "keep_top_k": 1})
    api = Api(factory=lambda cfg: clients, cwd=str(tmp_path))
    result = api.capacity(cfg)
    assert result["used_vcpus"] == 4
    assert result["required_peak_vcpus"] == 12
    assert result["problems"]  # each type alone would fit, combined 4 + 12 does not.
    assert result["max_launch_count"] is None
    assert len(result["groups"]) == 1


def test_distinct_quota_groups_are_checked_separately(tmp_path):
    clients = _factory()(None)
    class Quotas:
        def get_service_quota(self, **kwargs):
            return {"Quota": {"Value": 100}}
        def list_service_quotas(self, **kwargs):
            return {"Quotas": [{"QuotaName": "Running On-Demand G and VT instances", "Value": 1}]}
    clients["service-quotas"] = Quotas()
    cfg = load_config(overrides={"instance_groups": [{"instance_type": "t3.nano", "count": 1},
                                                    {"instance_type": "g9.xlarge", "count": 1}], "max_rounds": 1})
    result = Api(factory=lambda cfg: clients, cwd=str(tmp_path)).capacity(cfg)
    assert len(result["groups"]) == 2
    assert len(result["problems"]) == 1 and result["problems"][0].startswith("g ")


def test_later_type_failure_terminates_previously_launched_type():
    class EC2(_DemoEc2):
        def run_instances(self, **kwargs):
            if kwargs["InstanceType"] == "t4g.nano":
                raise _err("InsufficientInstanceCapacity")
            return {"Instances": [{"InstanceId": "i-x86"}]}
        def terminate_instances(self, InstanceIds):
            self.calls.append(InstanceIds)
    ec2 = EC2()
    cfg = load_config(overrides={"instance_groups": [{"instance_type": "t3.nano", "count": 1},
                                                    {"instance_type": "t4g.nano", "count": 1}]})
    templates = prepare_launch(ec2, _DemoSsm(), cfg)
    with pytest.raises(ClientError):
        Ec2Manager(ec2).launch(2, "xb-fail", 1, Infra("s", "g", "p", "", templates), cfg.instance_type)
    assert ec2.calls == [["i-x86"]]
