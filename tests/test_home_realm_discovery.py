import pytest

import home_realm_discovery as hrd


def index(**over):
    args = {"cache_ttl": 600}
    args.update(over)
    return hrd.RealmDiscoveryIndex(**args)


def add(idx, tenant="t1", domain="corp.example", idp="idp-1", **over):
    kwargs = {"verified": True, "priority": 0, "active": True}
    kwargs.update(over)
    return idx.add_domain(tenant, domain, idp, **kwargs)


def test_exact_email_resolves_tenant_and_idp():
    idx = index()
    add(idx)
    result = idx.resolve("alice@corp.example", 0)
    assert result["status"] == "ok"
    assert result["tenant_id"] == "t1"
    assert result["idp_id"] == "idp-1"
    assert result["domain"] == "corp.example"


def test_domain_is_case_insensitive_and_ignores_trailing_dot():
    idx = index()
    add(idx)
    result = idx.resolve("ALICE@Corp.Example.", 0)
    assert result["status"] == "ok"
    assert result["tenant_id"] == "t1"


def test_unicode_domain_matches_punycode_form():
    idx = index()
    add(idx, domain="例子.测试", idp="idn")
    unicode_result = idx.resolve("alice@例子.测试", 0)
    ascii_result = idx.resolve("alice@xn--fsqu00a.xn--0zwm56d", 0)
    assert unicode_result["idp_id"] == "idn"
    assert ascii_result["idp_id"] == "idn"


def test_confusable_cyrillic_domain_does_not_match_ascii_domain():
    idx = index()
    add(idx, domain="example.com", idp="idp-ascii")
    result = idx.resolve("alice@ex\u0430mple.com", 0)
    assert result["status"] == "not_found"
    assert result["candidates"] == []


def test_unverified_domain_is_ignored():
    idx = index()
    add(idx, verified=False)
    result = idx.resolve("alice@corp.example", 0)
    assert result["status"] == "not_found"


def test_inactive_tenant_is_ignored():
    idx = index()
    add(idx, active=False)
    result = idx.resolve("alice@corp.example", 0)
    assert result["status"] == "not_found"


def test_wildcard_matches_only_one_label():
    idx = index()
    add(idx, domain="*.corp.example", wildcard=True)
    assert idx.resolve("alice@a.corp.example", 0)["status"] == "ok"
    deep = idx.resolve("alice@a.b.corp.example", 0)
    assert deep["status"] == "not_found"


def test_wildcard_requires_explicit_enablement():
    idx = index()
    with pytest.raises(ValueError):
        idx.add_domain("t1", "*.corp.example", "idp-1", verified=True, wildcard=False)


def test_local_part_with_plus_or_dot_does_not_confuse_domain():
    idx = index()
    add(idx)
    plus = idx.resolve("alice+tag@corp.example", 0)
    dotted = idx.resolve("first.last@corp.example", 0)
    assert plus["domain"] == "corp.example"
    assert dotted["domain"] == "corp.example"
    assert plus["tenant_id"] == dotted["tenant_id"] == "t1"


def test_domain_claimed_by_two_tenants_returns_conflict_candidates():
    idx = index()
    add(idx, tenant="t2", idp="idp-2")
    add(idx, tenant="t1", idp="idp-1")
    result = idx.resolve("alice@corp.example", 0)
    assert result["status"] == "conflict"
    assert result["tenant_id"] is None
    assert [candidate["tenant_id"] for candidate in result["candidates"]] == ["t1", "t2"]


def test_multi_idp_tenant_uses_priority_then_idp_id():
    idx = index()
    add(idx, idp="idp-low", priority=1)
    add(idx, idp="idp-zed", priority=5)
    add(idx, idp="idp-alpha", priority=5)
    assert idx.resolve("alice@corp.example", 0)["idp_id"] == "idp-alpha"
    add(idx, idp="idp-top", priority=9)
    assert idx.resolve("alice@corp.example", 0)["idp_id"] == "idp-top"


def test_cache_hit_within_ttl_and_expiry_after_ttl():
    idx = index(cache_ttl=600)
    add(idx)
    first = idx.resolve("alice@corp.example", 0)
    second = idx.resolve("alice@corp.example", 100)
    third = idx.resolve("alice@corp.example", 700)
    assert first["cached"] is False
    assert second["cached"] is True
    assert third["cached"] is False
    assert third["idp_id"] == "idp-1"


def test_mapping_change_bumps_version_and_bypasses_cache():
    idx = index()
    add(idx, tenant="t1", idp="idp-1")
    first = idx.resolve("alice@corp.example", 0)
    add(idx, tenant="t2", idp="idp-2")
    second = idx.resolve("alice@corp.example", 1)
    assert second["status"] == "conflict"
    assert second["version"] == first["version"] + 1
    assert second["cached"] is False


def test_invalid_and_empty_email_returns_structured_error():
    idx = index()
    add(idx)
    assert idx.resolve("no-at-sign", 0)["error"] == "invalid_email"
    assert idx.resolve("", 0)["error"] == "invalid_email"
    empty_domain = idx.resolve("alice@", 0)
    assert empty_domain["status"] == "error"
    assert empty_domain["error"] == "invalid_domain"


def test_snapshot_is_isolated_and_counts_registrations():
    idx = index()
    add(idx, tenant="t1", idp="idp-1")
    add(idx, tenant="t2", domain="other.example", idp="idp-2")
    info = idx.snapshot()
    assert info["version"] == 2
    assert len(info["domains"]) == 2
    info["domains"].clear()
    assert len(idx.snapshot()["domains"]) == 2
