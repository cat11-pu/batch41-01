import pytest

import rstp_topology_guard as rstp


LOCAL_BRIDGE = "32768.00:11:22:33:44:55"


def guard(**overrides):
    options = {"bridge_id": LOCAL_BRIDGE}
    options.update(overrides)
    return rstp.TopologyGuard(**options)


def bpdu(**overrides):
    packet = {
        "version": 2,
        "root_id": LOCAL_BRIDGE,
        "sender_bridge": "32768.00:aa:bb:cc:dd:ee",
        "root_path_cost": 0,
        "message_age": 1,
        "max_age": 20,
        "tc": False,
        "port_id": "128.1",
    }
    packet.update(overrides)
    return packet


def test_lower_bridge_id_bpdu_triggers_root_reselection():
    node = guard()
    node.add_port("p1")
    result = node.receive_bpdu(
        bpdu(root_id="4096.00:aa:bb:cc:dd:ee", sender_bridge="4096.00:aa:bb:cc:dd:ee"),
        "p1")
    assert result["accepted"] is True
    assert node.root_id() == "4096.00:aa:bb:cc:dd:ee"
    assert node.root_port() == "p1"
    assert node.port_role("p1") == "Root"


def test_worse_root_bpdu_does_not_replace_current_root():
    node = guard()
    node.add_port("p1")
    node.receive_bpdu(bpdu(root_id="4096.00:aa:bb:cc:dd:ee",
                           sender_bridge="4096.00:aa:bb:cc:dd:ee"), "p1")
    node.receive_bpdu(bpdu(root_id="8192.00:aa:bb:cc:dd:ee",
                           sender_bridge="8192.00:aa:bb:cc:dd:ee"), "p1")
    assert node.root_id() == "4096.00:aa:bb:cc:dd:ee"
    assert node.root_port() == "p1"


def test_proposal_without_agreement_stays_in_learning():
    node = guard()
    node.add_port("p1")
    result = node.process_proposal("p1", agreement=False)
    assert result["state"] == "Learning"
    assert result["forwarding"] is False
    assert node.port_state("p1") == "Learning"


def test_agreement_completes_handshake_and_allows_forwarding():
    node = guard()
    node.add_port("p1")
    node.process_proposal("p1", agreement=False)
    result = node.process_proposal("p1", agreement=True)
    assert result["synced"] is True
    assert result["forwarding"] is True
    assert node.port_state("p1") == "Forwarding"


def test_bpdu_guard_protects_non_edge_switch_port():
    node = guard()
    node.add_port("uplink", edge=False, guard=True)
    result = node.receive_bpdu(bpdu(), "uplink")
    assert result["accepted"] is False
    assert result["reason"] == "bpdu guard"
    assert node.port_state("uplink") == "Disabled"


def test_root_guard_compares_bridge_id_not_port_id():
    node = guard()
    node.add_port("p-root", root_guard=True)
    blocked = node.receive_bpdu(
        bpdu(root_id="4096.00:aa:bb:cc:dd:ee",
             sender_bridge="4096.00:aa:bb:cc:dd:ee", port_id="128.1"),
        "p-root")
    assert blocked["accepted"] is False
    assert blocked["reason"] == "root guard"
    assert node.root_id() == LOCAL_BRIDGE
    allowed = node.receive_bpdu(
        bpdu(root_id="40960.00:aa:bb:cc:dd:ee",
             sender_bridge="40960.00:aa:bb:cc:dd:ee", port_id="1.1"),
        "p-root")
    assert allowed["accepted"] is True
    assert node.root_id() == LOCAL_BRIDGE


def test_own_bpdu_is_detected_as_a_loop_and_blocks_port():
    node = guard()
    node.add_port("p1")
    node.advance_port("p1")
    node.advance_port("p1")
    assert node.port_state("p1") == "Forwarding"
    result = node.receive_bpdu(bpdu(sender_bridge=LOCAL_BRIDGE), "p1")
    assert result["accepted"] is False
    assert result["reason"] == "own bpdu loop"
    assert result["loop"] is True
    assert node.port_state("p1") == "Discarding"


def test_message_age_above_max_age_is_rejected():
    node = guard()
    node.add_port("p1")
    expired = node.receive_bpdu(bpdu(message_age=21, max_age=20), "p1")
    assert expired["accepted"] is False
    assert expired["reason"] == "message age exceeded"
    fresh = node.receive_bpdu(bpdu(message_age=20, max_age=20), "p1")
    assert fresh["accepted"] is True


def test_path_cost_distinguishes_gigabit_and_ten_gigabit():
    node = guard()
    assert node.path_cost(1000) == 20000
    assert node.path_cost(10000) == 2000
    assert node.path_cost(1000) != node.path_cost(10000)


def test_version_one_bpdu_is_rejected_without_offset_crash():
    node = guard()
    node.add_port("p1")
    result = node.receive_bpdu({"version": 1}, "p1")
    assert result["accepted"] is False
    assert result["reason"] == "unsupported bpdu version"
    assert node.port_state("p1") == "Discarding"


def test_topology_change_flag_is_flooded_and_flushes_mac_table():
    node = guard()
    node.add_port("p1")
    result = node.receive_bpdu(bpdu(tc=True), "p1")
    assert result["accepted"] is True
    assert node.tc_events() == 1
    assert node.snapshot()["tc_flooded"] == 1
    assert node.snapshot()["mac_flush_pending"] is True


def test_topology_change_storm_is_rate_limited():
    node = guard(tc_limit=3)
    node.add_port("p1")
    for moment in (0.0, 0.1, 0.2, 0.3, 0.4):
        node.receive_bpdu(bpdu(tc=True), "p1", now=moment)
    assert node.tc_events() == 3
    assert node.snapshot()["tc_suppressed"] == 2


def test_port_cannot_jump_from_discarding_to_forwarding():
    node = guard()
    node.add_port("p1")
    assert node.port_state("p1") == "Discarding"
    first = node.advance_port("p1")
    assert first == "Learning"
    assert node.port_state("p1") != "Forwarding"
    second = node.advance_port("p1")
    assert second == "Forwarding"


def test_repeated_bpdu_keeps_alternate_role_stable():
    node = guard()
    node.add_port("p1")
    node.add_port("p2")
    node.receive_bpdu(bpdu(root_id="4096.00:aa:bb:cc:dd:ee",
                           sender_bridge="4096.00:aa:bb:cc:dd:ee"), "p1")
    node.receive_bpdu(bpdu(root_id="4096.00:aa:bb:cc:dd:ee",
                           sender_bridge="4096.00:aa:bb:cc:dd:ee"), "p2")
    role = node.port_role("p2")
    assert role == "Alternate"
    for _ in range(3):
        node.receive_bpdu(bpdu(root_id="4096.00:aa:bb:cc:dd:ee",
                               sender_bridge="4096.00:aa:bb:cc:dd:ee"), "p2")
        assert node.port_role("p2") == role


def test_bpdu_without_tc_flag_does_not_count_topology_change():
    node = guard()
    node.add_port("p1")
    node.receive_bpdu(bpdu(tc=False), "p1")
    assert node.tc_events() == 0
    assert node.snapshot()["mac_flush_pending"] is False
