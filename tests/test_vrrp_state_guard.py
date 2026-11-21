import pytest

import vrrp_state_guard as vrrp


def fsm(**overrides):
    options = {
        "vrid": 42,
        "priority": 100,
        "ip": "10.0.0.2",
        "advert_interval": 1.0,
        "preempt": True,
        "auth_key": "vrrp",
        "domain": "lan-a",
    }
    options.update(overrides)
    node = vrrp.VrrpFsm(**options)
    node.start(0.0)
    return node


def advert(**overrides):
    packet = {
        "vrid": 42,
        "priority": 100,
        "ip": "10.0.0.1",
        "interval": 1.0,
        "auth": "vrrp",
        "ttl": 255,
        "checksum": 1,
        "domain": "lan-a",
    }
    packet.update(overrides)
    return packet


def test_same_vrid_in_other_broadcast_domain_is_ignored():
    node = fsm(domain="lan-b")
    result = node.receive(advert(domain="lan-a", priority=150), 0.0)
    assert result["action"] == "ignore"
    assert result["reason"] == "wrong domain"
    assert node.current_state() == "BACKUP"
    assert node.tick(4.0)["state"] == "MASTER"


def test_equal_priority_is_broken_by_higher_ip():
    node = fsm(ip="10.0.0.2")
    result = node.receive(advert(ip="10.0.0.1", priority=100), 0.0)
    assert result["action"] == "preempt_pending"
    assert node.tick(0.7)["state"] == "MASTER"


def test_preempt_disabled_keeps_backup_role():
    node = fsm(priority=200, ip="10.0.0.2", preempt=False)
    result = node.receive(advert(priority=100, ip="10.0.0.1"), 0.0)
    assert result["action"] == "stay_backup"
    assert node.tick(1.0)["state"] == "BACKUP"


def test_slow_advert_interval_does_not_trigger_false_failover():
    node = fsm(preempt=False)
    node.receive(advert(priority=10, interval=2.0), 0.0)
    assert node.tick(4.0)["state"] == "BACKUP"
    assert node.tick(6.7)["state"] == "MASTER"


def test_forged_advert_with_bad_auth_cannot_depose_master():
    node = fsm()
    assert node.tick(4.0)["state"] == "MASTER"
    result = node.receive(advert(priority=255, auth="forged"), 4.1)
    assert result["reason"] == "bad auth"
    assert node.current_state() == "MASTER"


def test_virtual_mac_is_derived_from_vrid():
    assert fsm(vrid=42).virtual_mac() == "00:00:5e:00:01:2a"
    assert fsm(vrid=1).virtual_mac() == "00:00:5e:00:01:01"


def test_restart_waits_for_skew_and_master_down_interval():
    node = fsm()
    assert node.current_state() == "BACKUP"
    assert node.tick(3.5)["state"] == "BACKUP"
    assert node.tick(3.7)["state"] == "MASTER"


def test_advert_with_non_255_ttl_is_ignored():
    node = fsm()
    result = node.receive(advert(ttl=254, priority=150), 0.0)
    assert result["action"] == "ignore"
    assert result["reason"] == "bad ttl"
    assert node.current_state() == "BACKUP"


def test_advert_with_bad_checksum_is_ignored():
    node = fsm()
    result = node.receive(advert(checksum=0, priority=150), 0.0)
    assert result["action"] == "ignore"
    assert result["reason"] == "bad checksum"
    assert node.current_state() == "BACKUP"


def test_tracking_failure_lowers_priority_and_allows_step_down():
    node = fsm(priority=150, ip="10.0.0.1")
    assert node.tick(4.0)["state"] == "MASTER"
    assert node.set_tracking(False) == 140
    result = node.receive(advert(priority=145, ip="10.0.0.2"), 4.1)
    assert result["action"] == "step_down"
    assert node.current_state() == "BACKUP"


def test_split_brain_elects_one_master_and_one_garp_sender():
    peers = [
        {"ip": "10.0.0.1", "priority": 100, "domain": "lan-a"},
        {"ip": "10.0.0.2", "priority": 100, "domain": "lan-a"},
    ]
    low = fsm(ip="10.0.0.1")
    low_result = low.resolve_split_brain(peers)
    assert low_result["master"] == "10.0.0.2"
    assert low_result["garp_senders"] == ["10.0.0.2"]
    assert low.current_state() == "BACKUP"
    high = fsm(ip="10.0.0.2")
    high_result = high.resolve_split_brain(peers)
    assert high_result["master"] == "10.0.0.2"
    assert high.current_state() == "MASTER"


def test_priority_zero_advert_hands_over_after_skew():
    node = fsm()
    result = node.receive(advert(priority=0), 0.0)
    assert result["action"] == "master_release"
    assert node.tick(0.1)["state"] == "BACKUP"
    assert node.tick(0.7)["state"] == "MASTER"


def test_advert_for_another_vrid_is_ignored():
    node = fsm()
    result = node.receive(advert(vrid=99, priority=200), 0.0)
    assert result["action"] == "ignore"
    assert result["reason"] == "vrid mismatch"
    assert node.current_state() == "BACKUP"


def test_repeated_adverts_refresh_master_down_timer():
    node = fsm(preempt=False)
    for moment in (0.0, 1.0, 2.0):
        assert node.receive(advert(priority=10), moment)["action"] == "stay_backup"
    assert node.tick(3.5)["state"] == "BACKUP"
    assert node.tick(5.7)["state"] == "MASTER"


def test_higher_priority_advert_forces_master_to_step_down():
    node = fsm(priority=100, ip="10.0.0.2")
    assert node.tick(4.0)["state"] == "MASTER"
    result = node.receive(advert(priority=150, ip="10.0.0.1"), 4.1)
    assert result["action"] == "step_down"
    assert node.current_state() == "BACKUP"
