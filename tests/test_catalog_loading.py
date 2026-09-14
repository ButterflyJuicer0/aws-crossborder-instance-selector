import pytest

from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import _DemoEc2, _DemoSsm
from tests.test_web_api import _factory, _err


def test_options_returns_all_names_without_waiting_for_any_specifications(tmp_path):
    class SlowSpecs(_DemoEc2):
        def describe_instance_types(self, **kwargs):
            pytest.fail("Listing model names must not wait for specifications")
    clients = _factory()(None)
    clients["ec2"] = SlowSpecs()
    result = Api(factory=lambda cfg: clients, cwd=str(tmp_path)).options("ap-east-1")
    assert "t4g.nano" in {i["type"] for i in result["instance_types"]}
    assert not result["errors"] and result["instance_types_source"] == "aws"
    assert all(i["vcpu"] is None for i in result["instance_types"])


def test_refresh_failure_keeps_only_same_region_and_subnet_cache(tmp_path):
    class EC2(_DemoEc2):
        fail = False
        def describe_instance_type_offerings(self, **kwargs):
            if self.fail:
                raise _err("UnauthorizedOperation")
            return super().describe_instance_type_offerings(**kwargs)
    ec2 = EC2()
    clients = _factory()(None)
    clients["ec2"] = ec2
    api = Api(factory=lambda cfg: clients, cwd=str(tmp_path))
    first = api.options("ap-east-1")
    ec2.fail = True
    cached = api.options("ap-east-1", refresh=True)
    assert cached["instance_types"] == first["instance_types"]
    assert cached["instance_types_source"] == "aws-cache" and cached["errors"]
    for region, subnet in (("us-west-2", ""), ("ap-east-1", "subnet-new")):
        missing = api.options(region, subnet, refresh=True)
        assert not missing["instance_types"]
        assert missing["instance_types_source"] == "unavailable"


def test_selected_type_specs_survive_an_image_lookup_failure(tmp_path):
    class Denied(_DemoSsm):
        def get_parameters(self, **kwargs):
            raise _err("AccessDeniedException")
    clients = _factory()(None)
    clients["ssm"] = Denied()
    result = Api(factory=lambda cfg: clients, cwd=str(tmp_path)).images("ap-east-1", "t4g.nano")
    assert result["error"] and not result["images"]
    assert result["instance_spec"] == {"type": "t4g.nano", "vcpu": 2, "memory_gib": .5,
                                      "architectures": ["arm64"], "arch": "arm64"}
