import pytest

import vxlan_fdb_learner as vfl

MAC1 = "02:00:00:00:00:01"
MAC2 = "02:00:00:00:00:02"
MAC3 = "02:00:00:00:00:03"


def learner(**over):
    args = {"age_seconds": 300, "arp_ttl": 60}
    args.update(over)
    return vfl.FdbLearner(**args)


def test_learn_and_lookup_normalize_mac():
    bridge = learner()
    result = bridge.learn(100, "02:00:00:00:00:AA", "vtep-a", 1, 0)
    assert result["updated"] is True
    entry = bridge.lookup(100, "02:00:00:00:00:aa", 0)
    assert entry["vtep"] == "vtep-a"
    assert entry["mac"] == "02:00:00:00:00:aa"


def test_higher_sequence_migrates_mac():
    bridge = learner()
    bridge.learn(100, MAC1, "vtep-a", 10, 0)
    moved = bridge.learn(100, MAC1, "vtep-b", 11, 1)
    assert moved["updated"] is True
    assert bridge.lookup(100, MAC1, 1)["vtep"] == "vtep-b"


def test_stale_or_equal_sequence_does_not_move_back():
    bridge = learner()
    bridge.learn(100, MAC1, "vtep-a", 10, 0)
    bridge.learn(100, MAC1, "vtep-b", 11, 1)
    late = bridge.learn(100, MAC1, "vtep-a", 10, 2)
    equal = bridge.learn(100, MAC1, "vtep-a", 11, 2)
    assert late["updated"] is False
    assert late["reason"] == "stale_sequence"
    assert equal["updated"] is False
    assert bridge.lookup(100, MAC1, 2)["vtep"] == "vtep-b"


def test_same_mac_in_different_vnis_is_isolated():
    bridge = learner()
    bridge.learn(100, MAC1, "vtep-a", 1, 0)
    bridge.learn(200, MAC1, "vtep-b", 1, 0)
    assert bridge.lookup(100, MAC1, 0)["vtep"] == "vtep-a"
    assert bridge.lookup(200, MAC1, 0)["vtep"] == "vtep-b"
    assert bridge.lookup(300, MAC1, 0) is None


def test_controller_sync_keeps_static_and_removes_dynamic():
    bridge = learner()
    bridge.learn(100, MAC1, "vtep-a", 1, 0)
    bridge.learn(100, MAC2, "vtep-a", 1, 0, static=True)
    bridge.sync_controller(100, [{"mac": MAC1, "vtep": "vtep-b", "seq": 2}])
    assert bridge.lookup(100, MAC1, 0)["vtep"] == "vtep-b"
    assert bridge.lookup(100, MAC2, 0)["vtep"] == "vtep-a"
    bridge.sync_controller(100, [])
    assert bridge.lookup(100, MAC1, 0) is None
    static = bridge.lookup(100, MAC2, 0)
    assert static is not None
    assert static["static"] is True


def test_bum_replication_includes_late_vtep_and_excludes_source():
    bridge = learner()
    bridge.join_vtep(100, "vtep-a")
    bridge.join_vtep(100, "vtep-b")
    bridge.join_vtep(100, "vtep-c")
    assert bridge.bum_replicators(100, "vtep-a") == ["vtep-b", "vtep-c"]
    bridge.join_vtep(100, "vtep-d")
    assert bridge.bum_replicators(100, "vtep-a") == ["vtep-b", "vtep-c", "vtep-d"]


def test_arp_suppression_expires_and_can_relearn():
    bridge = learner(arp_ttl=60)
    bridge.learn_arp(100, "10.0.0.5", MAC1, 0)
    assert bridge.resolve_arp(100, "10.0.0.5", 60) == MAC1
    assert bridge.resolve_arp(100, "10.0.0.5", 61) is None
    bridge.learn_arp(100, "10.0.0.5", MAC2, 61)
    assert bridge.resolve_arp(100, "10.0.0.5", 62) == MAC2


def test_zero_udp_checksum_is_rejected():
    bridge = learner()
    with pytest.raises(ValueError):
        bridge.validate_encap({"udp_checksum": 0})
    assert bridge.validate_encap({"udp_checksum": 0x1234}) is True


def test_mtu_subtracts_vxlan_overhead():
    bridge = learner()
    assert bridge.mtu_for(1500) == 1450
    assert bridge.mtu_for(9000) == 8950
    assert bridge.mtu_for() == 1450


def test_overlay_logical_loop_is_detected():
    bridge = learner()
    assert bridge.detect_loop(["vtep-a", "vtep-b", "vtep-c", "vtep-a"]) is True
    assert bridge.detect_loop(["vtep-a", "vtep-b", "vtep-c"]) is False


def test_fdb_aging_uses_supplied_clock():
    bridge = learner(age_seconds=300)
    bridge.learn(100, MAC1, "vtep-a", 1, 0)
    assert bridge.lookup(100, MAC1, 300) is not None
    assert bridge.age(300) == 0
    assert bridge.age(301) == 1
    assert bridge.lookup(100, MAC1, 301) is None


def test_evpn_type2_mac_length_anomaly_is_rejected():
    bridge = learner()
    good = bytes([48]) + bytes.fromhex("020000000001") + (100).to_bytes(3, "big")
    parsed = bridge.parse_evpn_type2(good)
    assert parsed["mac"] == MAC1
    assert parsed["vni"] == 100
    with pytest.raises(ValueError):
        bridge.parse_evpn_type2(bytes([64]) + bytes.fromhex("020000000001") + (100).to_bytes(3, "big"))
    with pytest.raises(ValueError):
        bridge.parse_evpn_type2(good[:9])


def test_malformed_mac_and_vni_are_rejected():
    bridge = learner()
    with pytest.raises(ValueError):
        bridge.learn(0, MAC1, "vtep-a", 1, 0)
    with pytest.raises(ValueError):
        bridge.learn(100, "02:00:00:00:00", "vtep-a", 1, 0)
    with pytest.raises(ValueError):
        bridge.learn(100, "zz:00:00:00:00:01", "vtep-a", 1, 0)


def test_concurrent_learning_keeps_highest_sequence():
    import threading

    bridge = learner()
    barrier = threading.Barrier(8)

    def worker(seq):
        barrier.wait()
        bridge.learn(100, MAC1, "vtep-%d" % seq, seq, 0)

    threads = [threading.Thread(target=worker, args=(seq,)) for seq in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entry = bridge.lookup(100, MAC1, 0)
    assert entry["seq"] == 8
    assert entry["vtep"] == "vtep-8"


def test_snapshot_counts_and_is_isolated():
    bridge = learner()
    bridge.learn(100, MAC1, "vtep-a", 1, 0)
    bridge.learn(100, MAC2, "vtep-a", 1, 0, static=True)
    bridge.learn(200, MAC3, "vtep-b", 1, 0)
    bridge.learn_arp(100, "10.0.0.5", MAC1, 0)
    info = bridge.snapshot()
    assert info["entries"] == 3
    assert info["static"] == 1
    assert info["vnis"][100] == 2
    assert info["vnis"][200] == 1
    assert info["arp_entries"] == 1
    info["vnis"][100] = 99
    assert bridge.snapshot()["vnis"][100] == 2
