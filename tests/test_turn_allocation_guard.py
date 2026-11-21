import re

import pytest

import turn_allocation_guard as tag


def guard(**over):
    args = {"realm": "turn.example.com", "nonce_ttl": 300, "max_lifetime": 3600,
            "permission_ttl": 300, "relay_port_start": 49152}
    args.update(over)
    return tag.AllocationGuard(**args)


def request(nonce, **over):
    base = {
        "transport": "udp",
        "client_ip": "203.0.113.5",
        "client_port": 50000,
        "nonce": nonce,
        "requested_lifetime": 600,
        "length": 40,
        "actual_length": 40,
        "fingerprint_ok": True,
        "xor_mapped_address": {
            "family": 1,
            "address_length": 4,
            "ip": "203.0.113.5",
            "port": 50000,
        },
    }
    base.update(over)
    return base


def allocate(server, now=0, **over):
    nonce = server.issue_nonce(now)["nonce"]
    over.setdefault("client_port", 50000 + len(server.allocations))
    return server.allocate(request(nonce, **over), now)


def test_allocate_returns_id_and_clamps_lifetime():
    server = guard()
    result = allocate(server, requested_lifetime=100000)
    assert re.fullmatch(r"[0-9a-f]{32}", result["allocation_id"])
    assert result["lifetime"] == 3600
    assert result["transport"] == "udp"
    assert result["relay_port"] >= 49152


def test_tcp_relay_allocation_is_rejected():
    server = guard()
    nonce = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce, transport="tcp"), 0)


def test_nonce_must_be_known_fresh_and_single_use():
    server = guard(nonce_ttl=300)
    nonce = server.issue_nonce(0)["nonce"]
    server.allocate(request(nonce, client_port=50001), 0)
    with pytest.raises(ValueError):
        server.allocate(request(nonce, client_port=50002), 1)
    stale = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(stale, client_port=50003), 301)
    with pytest.raises(ValueError):
        server.allocate(request("0" * 32, client_port=50004), 0)


def test_duplicate_five_tuple_allocation_is_rejected():
    server = guard()
    allocate(server, client_port=50001)
    nonce = server.issue_nonce(1)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce, client_port=50001), 1)


def test_permission_is_bound_to_the_requested_peer():
    server = guard()
    allocation = allocate(server)
    server.create_permission(allocation["allocation_id"], "8.8.8.8", 0)
    assert server.send(allocation["allocation_id"], "8.8.8.8", 1) is True
    assert server.send(allocation["allocation_id"], "1.1.1.1", 1) is False


def test_loopback_and_private_peers_are_rejected():
    server = guard()
    allocation = allocate(server)
    for peer in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1"):
        with pytest.raises(ValueError):
            server.create_permission(allocation["allocation_id"], peer, 0)
    assert server.send(allocation["allocation_id"], "127.0.0.1", 0) is False


def test_channel_conflict_is_rejected_and_same_peer_is_idempotent():
    server = guard()
    allocation = allocate(server)
    first = server.channel_bind(allocation["allocation_id"], 0x4000, "8.8.8.8", 0)
    again = server.channel_bind(allocation["allocation_id"], 0x4000, "8.8.8.8", 1)
    with pytest.raises(ValueError):
        server.channel_bind(allocation["allocation_id"], 0x4000, "1.1.1.1", 2)
    assert first["channel"] == again["channel"] == 0x4000


def test_declared_length_larger_than_payload_is_rejected():
    server = guard()
    nonce = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce, length=60, actual_length=40), 0)


def test_fingerprint_failure_is_rejected():
    server = guard()
    nonce = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce, fingerprint_ok=False), 0)


def test_xor_mapped_family_and_length_must_agree():
    server = guard()
    nonce = server.issue_nonce(0)["nonce"]
    bad_length = {
        "family": 1,
        "address_length": 16,
        "ip": "203.0.113.5",
        "port": 50000,
    }
    with pytest.raises(ValueError):
        server.allocate(request(nonce, xor_mapped_address=bad_length), 0)
    nonce2 = server.issue_nonce(0)["nonce"]
    bad_family = {
        "family": 2,
        "address_length": 16,
        "ip": "203.0.113.5",
        "port": 50000,
    }
    with pytest.raises(ValueError):
        server.allocate(request(nonce2, xor_mapped_address=bad_family), 0)


def test_invalid_lifetime_is_rejected():
    server = guard()
    nonce = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce, requested_lifetime=0), 0)
    nonce2 = server.issue_nonce(0)["nonce"]
    with pytest.raises(ValueError):
        server.allocate(request(nonce2, requested_lifetime=-5), 0)


def test_expire_sweeps_all_allocations():
    server = guard(max_lifetime=60)
    for index in range(5):
        allocate(server, client_port=50001 + index, requested_lifetime=60)
    assert server.snapshot()["allocations"] == 5
    assert server.expire(59) == 0
    assert server.expire(60) == 5
    assert server.snapshot()["allocations"] == 0


def test_expired_permission_blocks_send_and_can_be_refreshed():
    server = guard(permission_ttl=300)
    allocation = allocate(server)
    server.create_permission(allocation["allocation_id"], "8.8.8.8", 0)
    assert server.send(allocation["allocation_id"], "8.8.8.8", 299) is True
    assert server.send(allocation["allocation_id"], "8.8.8.8", 300) is False
    server.create_permission(allocation["allocation_id"], "8.8.8.8", 300)
    assert server.send(allocation["allocation_id"], "8.8.8.8", 301) is True


def test_ice_role_conflict_is_resolved_by_tie_breaker():
    server = guard()
    assert server.ice_role("controlling", "controlling", 100, 50) == "controlling"
    assert server.ice_role("controlling", "controlling", 10, 50) == "controlled"
    assert server.ice_role("controlled", "controlled", 100, 50) == "controlling"
    assert server.ice_role("controlled", "controlled", 10, 50) == "controlled"
    assert server.ice_role("controlling", "controlled", 10, 50) == "controlling"


def test_snapshot_counts_and_allocations_are_isolated():
    server = guard()
    first = allocate(server, client_port=50001)
    second = allocate(server, client_port=50002)
    assert first["allocation_id"] != second["allocation_id"]
    assert first["relay_port"] != second["relay_port"]
    info = server.snapshot()
    assert info["allocations"] == 2
    info["allocations"] = 99
    assert server.snapshot()["allocations"] == 2
