import pytest

import lacp_bundle_guard as lbg

OUR_SYSTEM = "aa:bb:cc:dd:ee:ff"
OUR_PRIORITY = 100
PEER_SYSTEM = "00:11:22:33:44:55"


def bundle(**over):
    args = {
        "group_id": "lag1",
        "system_id": OUR_SYSTEM,
        "system_priority": OUR_PRIORITY,
        "mode": "active",
        "timeout": "short",
        "min_links": 1,
        "cooldown": 5,
        "required_speed": 10000,
    }
    args.update(over)
    return lbg.BundleGuard(**args)


def lacpdu(actor_state=None, actor_system=PEER_SYSTEM, actor_priority=32768,
           partner_system=OUR_SYSTEM, partner_priority=OUR_PRIORITY,
           timeout="short", source_mac=None, tlv_length=110):
    state = {
        "activity": True,
        "timeout": timeout,
        "aggregation": True,
        "synchronization": True,
        "collecting": True,
        "distributing": True,
        "defaulted": False,
    }
    if actor_state:
        state.update(actor_state)
    return {
        "source_mac": source_mac or actor_system,
        "tlv_length": tlv_length,
        "actor": {
            "system_id": actor_system,
            "system_priority": actor_priority,
            "key": 1,
            "port": 1,
            "state": dict(state),
        },
        "partner": {
            "system_id": partner_system,
            "system_priority": partner_priority,
            "key": 1,
            "port": 1,
            "state": dict(state),
        },
    }


def ready_one(port="p1"):
    guard = bundle()
    guard.add_member(port, 10000)
    guard.recv_lacpdu(port, lacpdu(), 0)
    return guard


def test_valid_lacpdu_selects_member():
    guard = ready_one()
    state = guard.member_state("p1", 0)
    assert state["eligible"] is True
    assert state["synchronized"] is True
    assert state["reason"] is None
    assert guard.select(0) == ["p1"]


def test_defaulted_member_is_not_selected():
    guard = bundle()
    guard.add_member("p1", 10000)
    guard.recv_lacpdu("p1", lacpdu({"defaulted": True}), 0)
    state = guard.member_state("p1", 0)
    assert state["eligible"] is False
    assert state["reason"] == "defaulted"
    assert guard.select(0) == []


def test_collecting_and_distributing_are_both_required():
    guard = bundle()
    guard.add_member("p1", 10000)
    guard.recv_lacpdu("p1", lacpdu({"distributing": False}), 0)
    state = guard.member_state("p1", 0)
    assert state["eligible"] is False
    assert state["reason"] == "not_distributing"
    assert state["collecting"] is True
    assert state["distributing"] is False


def test_partner_system_priority_must_match():
    guard = bundle()
    guard.add_member("p1", 10000)
    result = guard.recv_lacpdu("p1", lacpdu(partner_priority=200), 0)
    assert result["synchronized"] is False
    assert result["reason"] == "partner_priority_mismatch"
    assert guard.member_state("p1", 0)["reason"] == "partner_priority_mismatch"


def test_partner_system_id_must_match():
    guard = bundle()
    guard.add_member("p1", 10000)
    result = guard.recv_lacpdu(
        "p1", lacpdu(partner_system="11:22:33:44:55:66"), 0
    )
    assert result["synchronized"] is False
    assert result["reason"] == "partner_system_mismatch"


def test_timeout_mode_mismatch_is_not_synchronized():
    guard = bundle(timeout="short")
    guard.add_member("p1", 10000)
    result = guard.recv_lacpdu("p1", lacpdu(timeout="long"), 0)
    assert result["synchronized"] is False
    assert result["reason"] == "timeout_mismatch"
    assert guard.select(0) == []


def test_malformed_tlv_length_is_rejected():
    guard = bundle()
    guard.add_member("p1", 10000)
    with pytest.raises(ValueError):
        guard.recv_lacpdu("p1", lacpdu(tlv_length=40), 0)


def test_spoofed_source_mac_is_rejected():
    guard = bundle()
    guard.add_member("p1", 10000)
    with pytest.raises(ValueError):
        guard.recv_lacpdu(
            "p1", lacpdu(source_mac="aa:aa:aa:aa:aa:aa"), 0
        )


def test_speed_mismatch_member_stays_standby():
    guard = bundle(required_speed=10000)
    guard.add_member("p1", 10000)
    guard.add_member("p2", 1000)
    guard.recv_lacpdu("p1", lacpdu(), 0)
    guard.recv_lacpdu("p2", lacpdu(), 0)
    assert guard.select(0) == ["p1"]
    state = guard.member_state("p2", 0)
    assert state["eligible"] is False
    assert state["reason"] == "speed_mismatch"


def test_min_links_must_be_met_for_aggregate_up():
    guard = bundle(min_links=2)
    guard.add_member("p1", 10000)
    guard.recv_lacpdu("p1", lacpdu(), 0)
    info = guard.aggregate_status(0)
    assert info["up"] is False
    assert info["reason"] == "min_links"
    assert info["selected"] == ["p1"]


def test_static_and_dynamic_members_cannot_be_mixed():
    guard = bundle(mode="active")
    with pytest.raises(ValueError):
        guard.add_member("p1", 10000, mode="static")


def test_flap_cooldown_blocks_then_recovers():
    guard = ready_one()
    guard.flap("p1", 0)
    blocked = guard.member_state("p1", 1)
    assert blocked["eligible"] is False
    assert blocked["reason"] == "cooldown"
    guard.recv_lacpdu("p1", lacpdu(), 6)
    assert guard.select(6) == ["p1"]


def test_mac_learning_is_synced_to_selected_members():
    guard = bundle()
    guard.add_member("p1", 10000)
    guard.add_member("p2", 10000)
    guard.recv_lacpdu("p1", lacpdu(), 0)
    guard.recv_lacpdu("p2", lacpdu(), 0)
    guard.learn_mac("02:00:00:00:00:AA", "p1", 0)
    table = guard.mac_table()
    assert table["02:00:00:00:00:aa"] == ["p1", "p2"]


def test_restart_keeps_group_id_and_forces_renegotiation():
    guard = ready_one()
    before = guard.snapshot()
    guard.restart(10)
    after = guard.snapshot()
    assert after["group_id"] == before["group_id"] == "lag1"
    assert guard.select(10) == []
    guard.recv_lacpdu("p1", lacpdu(), 11)
    assert guard.select(11) == ["p1"]


def test_member_expiry_boundary_and_recovery():
    guard = ready_one()
    at_boundary = guard.member_state("p1", 3)
    expired = guard.member_state("p1", 4)
    assert at_boundary["expired"] is False
    assert at_boundary["eligible"] is True
    assert expired["expired"] is True
    assert expired["reason"] == "expired"
    assert guard.select(4) == []
    guard.recv_lacpdu("p1", lacpdu(), 5)
    assert guard.select(5) == ["p1"]
