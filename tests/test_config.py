import pytest
import yaml

from crossborder_selector.config import load_config, DEFAULTS, KNOWN_BACKENDS, ISPS


def test_defaults_without_file():
    cfg = load_config(None)
    assert cfg.region == "ap-east-1"
    assert cfg.batch_size == 10 and cfg.max_rounds == 3 and cfg.keep_top_k == 1
    assert cfg.backends["reverse"]["enabled"] is True
    assert cfg.backends["ripeatlas"]["enabled"] is False
    assert set(cfg.weights["isps"]) == set(ISPS)
    # 三网 DNS 目标默认走 :53（host:port 形式）
    assert cfg.backends["reverse"]["targets"]["telecom"] == ["114.114.114.114:53", "www.189.cn"]
    assert cfg.backends["reverse"]["targets"]["mobile"] == ["221.130.33.52:53", "www.10086.cn"]


def test_file_deep_merges_over_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"batch_size": 4, "backends": {"reverse": {"ping_count": 3}}}))
    cfg = load_config(str(p))
    assert cfg.batch_size == 4
    assert cfg.backends["reverse"]["ping_count"] == 3
    assert cfg.backends["reverse"]["enabled"] is True  # 未覆盖的键保留默认


def test_overrides_and_backend_toggles():
    cfg = load_config(None, {"region": "ap-northeast-1", "enable_backends": ["itdog"],
                             "disable_backends": ["globalping"]})
    assert cfg.region == "ap-northeast-1"
    assert cfg.backends["itdog"]["enabled"] is True
    assert cfg.backends["globalping"]["enabled"] is False


@pytest.mark.parametrize("bad", [
    {"weights": {"isps": {"telecom": 1.0, "unicom": 1.0, "mobile": 1.0, "extra": 1.0}}},
    {"weights": {"lat_good_ms": 300, "lat_bad_ms": 60}},
    {"keep_top_k": 0},
    {"batch_size": 0},
    {"max_rounds": 0},
    {"backends": {"reverse": {"targets": {"telecom": [], "unicom": ["x"], "mobile": ["y"]}}}},
    {"weights": {"backends": {"bogus": 1.0}}},
])
def test_validation_errors(bad):
    with pytest.raises(ValueError):
        load_config(None, bad)


def test_unknown_enable_backend_rejected():
    with pytest.raises(ValueError):
        load_config(None, {"enable_backends": ["nope"]})


def test_known_backends_constant():
    assert KNOWN_BACKENDS == ("reverse", "globalping", "ripeatlas", "itdog")


def test_final_retention_can_exceed_five_but_not_total_candidates():
    assert load_config(overrides={"batch_size": 10, "max_rounds": 5, "keep_top_k": 50}).keep_top_k == 50
    with pytest.raises(ValueError):
        load_config(overrides={"batch_size": 50, "max_rounds": 2, "keep_top_k": 51})
    with pytest.raises(ValueError, match="最终保留数量"):
        load_config(overrides={"batch_size": 2, "max_rounds": 2, "keep_top_k": 5})
