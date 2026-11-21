import re

import pytest

import wg_handshake_guard as wg


def guard(**over):
    args = {"psk": None, "replay_window": 2000, "max_handshake_age": 120}
    args.update(over)
    return wg.HandshakeGuard(**args)


def handshake(store, index=1, peer="peer-a", timestamp=0, now=0):
    started = store.initiate(index, peer, timestamp, now)
    finished = store.respond(index, peer, timestamp, now)
    return started, finished


def test_initiate_accepts_fresh_timestamp():
    store = guard()
    result = store.initiate(1, "peer-a", 100, 100)
    assert result["ok"] is True
    assert result["index"] == 1
    assert result["reason"] is None


def test_initiate_rejects_stale_timestamp():
    store = guard(max_handshake_age=120)
    result = store.initiate(1, "peer-a", 0, 121)
    assert result["ok"] is False
    assert result["reason"] == "stale_timestamp"


def test_initiate_rejects_replayed_timestamp():
    store = guard()
    store.initiate(1, "peer-a", 100, 100)
    replay = store.initiate(2, "peer-a", 100, 101)
    assert replay["ok"] is False
    assert replay["reason"] == "replay"


def test_cookie_reply_requires_valid_mac1():
    store = guard()
    bad = store.cookie_reply("198.51.100.10", "00" * 16, valid=False)
    good = store.cookie_reply("198.51.100.10", "ab" * 16, valid=True)
    assert bad["ok"] is False
    assert bad["cookie"] is None
    assert good["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", good["cookie"])


def test_same_handshake_response_is_not_processed_twice():
    store = guard()
    started, first = handshake(store)
    second = store.respond(1, "peer-a", 0, 1)
    assert started["ok"] is True
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == "duplicate_index"
    assert store.snapshot()["sessions"] == 1


def test_psk_changes_derived_key_material():
    with_psk = guard(psk="shared-secret")
    without = guard()
    _, first = handshake(with_psk)
    _, second = handshake(without)
    assert first["key_material"] != second["key_material"]
    assert re.fullmatch(r"[0-9a-f]{64}", first["key_material"])


def test_allowed_ip_uses_longest_prefix():
    store = guard()
    store.allow_ip("peer-a", "0.0.0.0/0")
    store.allow_ip("peer-a", "10.0.0.0/8")
    store.allow_ip("peer-a", "10.1.0.0/16")
    assert store.select_allowed_ip("peer-a", "10.1.2.3") == "10.1.0.0/16"
    assert store.select_allowed_ip("peer-a", "10.2.2.3") == "10.0.0.0/8"
    assert store.select_allowed_ip("peer-a", "8.8.8.8") == "0.0.0.0/0"


def test_ipv4_and_ipv6_allowed_trees_do_not_mix():
    store = guard()
    store.allow_ip("v4-peer", "10.0.0.0/8")
    store.allow_ip("v6-peer", "2001:db8::/32")
    assert store.select_allowed_ip("v4-peer", "10.1.1.1") == "10.0.0.0/8"
    assert store.select_allowed_ip("v4-peer", "2001:db8::1") is None
    assert store.select_allowed_ip("v6-peer", "2001:db8::1") == "2001:db8::/32"
    assert store.select_allowed_ip("v6-peer", "10.1.1.1") is None


def test_transport_requires_known_receiver_index():
    store = guard()
    assert store.transport(999, 1, 0) is False
    handshake(store)
    assert store.transport(1, 1, 1) is True
    assert store.transport(1, 1, 1) is False


def test_replay_window_survives_restart(tmp_path):
    path = str(tmp_path / "wg.json")
    store = guard()
    handshake(store)
    assert store.transport(1, 1, 1) is True
    assert store.transport(1, 3, 1) is True
    store.flush(path)
    reborn = wg.HandshakeGuard.load(path)
    assert reborn.transport(1, 3, 2) is False
    assert reborn.transport(1, 4, 2) is True


def test_key_rotation_keeps_old_key_during_grace():
    store = guard()
    first = store.rotate_key("peer-a", 0, grace=60)
    assert store.decrypt("peer-a", first["key_id"], 0) is True
    second = store.rotate_key("peer-a", 10, grace=60)
    assert store.decrypt("peer-a", first["key_id"], 20) is True
    assert store.decrypt("peer-a", second["key_id"], 20) is True
    assert store.decrypt("peer-a", first["key_id"], 70) is False
    assert store.decrypt("peer-a", second["key_id"], 70) is True


def test_keepalive_is_rate_limited():
    store = guard()
    assert store.keepalive("peer-a", 0)["ok"] is True
    limited = store.keepalive("peer-a", 5)
    assert limited["ok"] is False
    assert limited["reason"] == "rate_limited"
    assert store.keepalive("peer-a", 10)["ok"] is True


def test_handshake_message_lengths_are_validated():
    store = guard()
    assert store.validate_message("initiation", 148) is True
    assert store.validate_message("response", 92) is True
    assert store.validate_message("cookie", 64) is True
    assert store.validate_message("transport", 32) is True
    with pytest.raises(ValueError):
        store.validate_message("initiation", 149)
    with pytest.raises(ValueError):
        store.validate_message("transport", 16)


def test_duplicate_index_with_different_peer_is_rejected():
    store = guard()
    store.initiate(7, "peer-a", 0, 0)
    with pytest.raises(ValueError):
        store.initiate(7, "peer-b", 0, 0)


def test_worker_loads_session_table_and_snapshot_is_isolated(tmp_path):
    path = str(tmp_path / "wg.json")
    first_worker = guard()
    handshake(first_worker)
    first_worker.flush(path)
    second_worker = wg.HandshakeGuard.load(path)
    assert second_worker.transport(1, 1, 1) is True
    snapshot = second_worker.snapshot()
    assert snapshot["sessions"] == 1
    assert snapshot["indexes"] == [1]
    snapshot["sessions"] = 99
    assert second_worker.snapshot()["sessions"] == 1
    assert first_worker.snapshot()["sessions"] == 1
