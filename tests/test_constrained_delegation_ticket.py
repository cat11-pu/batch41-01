import pytest

import constrained_delegation_ticket as cdt


def make_arbiter(**kwargs):
    return cdt.DelegationTicketArbiter(
        ["svc-a", "svc-b", "svc-edge", "svc-mid"], **kwargs
    )


def ticket(arbiter, **overrides):
    data = {
        "user": "alice",
        "service": "svc-a",
        "flags": ["forwardable"],
        "pac_groups": ["group-a"],
        "transit": [],
        "origin": "svc-front",
        "account_delegatable": True,
        "realm": "corp.local",
        "referral": False,
        "proxy": False,
        "version": 1,
    }
    data.update(overrides)
    if "pac_signature" not in overrides:
        data["pac_signature"] = arbiter.sign_pac(data["pac_groups"])
    return data


def test_forwardable_ticket_is_allowed_with_chain():
    arbiter = make_arbiter(max_depth=2)
    result = arbiter.authorize(ticket(arbiter, transit=["svc-edge"]))
    assert result["allowed"] is True
    assert result["reason"] == "ok"
    assert result["chain"] == ["svc-edge", "svc-a"]
    assert result["origin"] == "svc-front"


def test_ticket_without_forwardable_flag_is_denied():
    arbiter = make_arbiter()
    result = arbiter.authorize(ticket(arbiter, flags=[]))
    assert result["allowed"] is False
    assert result["reason"] == "missing_forwardable"


def test_proxy_use_requires_proxy_flag():
    arbiter = make_arbiter()
    result = arbiter.authorize(ticket(arbiter, proxy=True, flags=["forwardable"]))
    assert result["allowed"] is False
    assert result["reason"] == "proxy_not_allowed"


def test_service_whitelist_is_exact_not_prefix():
    arbiter = cdt.DelegationTicketArbiter(["svc"])
    result = arbiter.authorize(ticket(arbiter, service="svc-evil"))
    assert result["allowed"] is False
    assert result["reason"] == "service_not_allowed"


def test_service_name_with_spaces_is_not_trimmed_into_match():
    arbiter = make_arbiter()
    result = arbiter.authorize(ticket(arbiter, service=" svc-a "))
    assert result["allowed"] is False
    assert result["reason"] == "service_not_allowed"


def test_trimmed_pac_groups_break_the_signature():
    arbiter = make_arbiter()
    data = ticket(arbiter, pac_groups=["group-a", "group-b"])
    data["pac_groups"] = ["group-a"]
    result = arbiter.authorize(data)
    assert result["allowed"] is False
    assert result["reason"] == "pac_signature_invalid"


def test_non_delegatable_service_account_is_denied():
    arbiter = make_arbiter()
    result = arbiter.authorize(ticket(arbiter, account_delegatable=False))
    assert result["allowed"] is False
    assert result["reason"] == "account_not_delegatable"


def test_delegation_depth_is_limited():
    arbiter = make_arbiter(max_depth=1)
    result = arbiter.authorize(ticket(arbiter, transit=["svc-edge"]))
    assert result["allowed"] is False
    assert result["reason"] == "depth_exceeded"


def test_unknown_transit_service_is_denied():
    arbiter = make_arbiter(max_depth=3)
    result = arbiter.authorize(ticket(arbiter, transit=["svc-unknown"]))
    assert result["allowed"] is False
    assert result["reason"] == "transit_not_allowed"


def test_repeated_transit_hop_is_denied():
    arbiter = make_arbiter(max_depth=3)
    result = arbiter.authorize(ticket(arbiter, transit=["svc-edge", "svc-edge"]))
    assert result["allowed"] is False
    assert result["reason"] == "transit_loop"


def test_cross_realm_ticket_requires_referral():
    arbiter = make_arbiter(trusted_realms=("partner.local",))
    result = arbiter.authorize(ticket(arbiter, realm="partner.local", referral=False))
    assert result["allowed"] is False
    assert result["reason"] == "referral_required"


def test_cache_is_isolated_per_user_and_service():
    arbiter = make_arbiter()
    decision = {"allowed": True, "reason": "ok"}
    arbiter.cache_decision("alice", "svc-a", decision, version=1)
    assert arbiter.lookup_decision("alice", "svc-a", 1) == decision
    assert arbiter.lookup_decision("bob", "svc-a", 1) is None
    assert arbiter.lookup_decision("alice", "svc-b", 1) is None


def test_cache_version_mismatch_returns_none():
    arbiter = make_arbiter()
    arbiter.cache_decision("alice", "svc-a", {"allowed": True}, version=1)
    assert arbiter.lookup_decision("alice", "svc-a", 2) is None


def test_audit_records_origin_and_source_chain():
    arbiter = make_arbiter(max_depth=2)
    arbiter.authorize(ticket(arbiter, transit=["svc-edge"]))
    entry = arbiter.audit()[-1]
    assert entry["chain"] == ["svc-edge", "svc-a"]
    assert entry["origin"] == "svc-front"
    assert entry["allowed"] is True


def test_parse_flags_normalizes_and_rejects_unknown_flags():
    arbiter = make_arbiter()
    assert arbiter.parse_flags("Forwardable, Proxy") == frozenset(
        {"forwardable", "proxy"}
    )
    with pytest.raises(ValueError):
        arbiter.parse_flags("forwardable,teleport")
