import pytest

from crossborder_selector.aws.catalog import image_catalog
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import _DemoEc2, _DemoSsm
from tests.test_web_api import _err, _factory


@pytest.mark.parametrize("instance_type,architecture,path_arch", [
    ("t3.nano", "x86_64", "amd64"), ("t4g.nano", "arm64", "arm64"),
])
def test_public_images_match_real_instance_architecture(instance_type, architecture, path_arch):
    class SSM(_DemoSsm):
        def get_parameters(self, Names):
            assert len(Names) == 3
            assert f"/{path_arch}/hvm/" in Names[1]
            return super().get_parameters(Names)
    result = image_catalog(_DemoEc2(), SSM(), instance_type)
    assert [i["name"] for i in result["images"]] == ["Amazon Linux 2023", "Ubuntu 24.04 LTS", "Ubuntu 22.04 LTS"]
    assert all(i["architecture"] == architecture and i["root_volume_size_gib"] == 8 for i in result["images"])


def test_missing_parameter_and_incompatible_image_are_not_offered():
    class SSM(_DemoSsm):
        def get_parameters(self, Names):
            return super().get_parameters(Names[:2])
    class EC2(_DemoEc2):
        def describe_images(self, **kwargs):
            result = super().describe_images(**kwargs)
            for image in result["Images"]:
                if "ubuntu" in image["ImageId"]:
                    image["Architecture"] = "arm64"
            return result
    result = image_catalog(EC2(), SSM(), "t3.nano")
    assert [i["key"] for i in result["images"]] == ["al2023"]
    assert len(result["notes"]) == 2


def test_image_cache_is_scoped_to_region_and_type_and_errors_can_retry(tmp_path):
    calls = []
    clients = _factory()(None)
    def factory(cfg):
        calls.append((cfg.region, cfg.instance_type))
        return clients
    api = Api(factory=factory, cwd=str(tmp_path))
    assert len(api.images("ap-east-1", "t3.nano")["images"]) == 3
    api.images("ap-east-1", "t3.nano")
    api.images("us-east-1", "t3.nano")
    api.images("ap-east-1", "t4g.nano")
    assert len(calls) == 3
    class Denied:
        def get_parameters(self, **kwargs):
            raise _err("AccessDeniedException")
    clients["ssm"] = Denied()
    failed = api.images("us-west-2", "t3.nano")
    assert not failed["images"] and "AccessDeniedException" in failed["error"]
    clients["ssm"] = _DemoSsm()
    assert len(api.images("us-west-2", "t3.nano")["images"]) == 3
