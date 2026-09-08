from crossborder_selector.web.pricing import INSTANCE_CATALOG, hourly_for, estimate, REGIONS, IPV4_HOURLY_USD


def test_catalog_has_expected_types():
    types = {i["type"] for i in INSTANCE_CATALOG}
    assert {"t3.nano", "t4g.nano", "t3.medium", "m6g.medium", "c6g.medium"} <= types
    assert all({"type", "vcpu", "memory_gib", "arch", "hourly_usd"} <= set(i) for i in INSTANCE_CATALOG)


def test_hourly_and_estimate():
    assert hourly_for("t3.nano") == 0.0066 and hourly_for("zz.huge") == 0.02
    e = estimate("t3.nano", 20, 3)
    assert e["estimated_minutes"] == 24
    assert e["estimated_cost_usd"] == round(20 * 3 * (0.0066 + IPV4_HOURLY_USD) * 8 / 60, 3)
    assert "估算" in e["note"]


def test_regions_include_hk_first():
    assert REGIONS[0]["code"] == "ap-east-1" and any(r["code"] == "ap-northeast-1" for r in REGIONS)
