import pytest

import quic_path_migrator as qpm


def build(**kwargs):
    migrator = qpm.PathMigrator(**kwargs)
    migrator.add_path("p1", ("10.0.0.1", 4433), cwnd=10.0, rtt=0.05,
                      worker="w1")
    migrator.add_path("p2", ("10.0.0.2", 4433), cwnd=40.0, rtt=0.02,
                      worker="w2")
    migrator.activate("p1")
    return migrator


def packet(path_id, addr, pn=1, now=0.0, **extra):
    data = {"path_id": path_id, "addr": addr, "pn": pn, "now": now}
    data.update(extra)
    return data


def test_unvalidated_new_path_is_challenged_not_switched():
    migrator = build()
    result = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    assert result["action"] == "challenge"
    assert result["token"]
    assert migrator.active_path() == "p1"


def test_nat_rebinding_same_ip_new_port_is_accepted():
    migrator = build()
    result = migrator.on_packet(packet("p1", ("10.0.0.1", 55000), pn=7))
    assert result["action"] == "nat_rebind"
    assert migrator.path_addr("p1") == ("10.0.0.1", 55000)
    assert migrator.active_path() == "p1"


def test_new_ip_is_not_committed_until_validation_finishes():
    migrator = build()
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    verdict = migrator.validate("p2", challenge["token"])
    assert verdict["validated"] is True
    assert migrator.active_path() == "p1"


def test_commit_before_validation_is_refused():
    migrator = build()
    migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    result = migrator.commit("p2")
    assert result["committed"] is False
    assert result["reason"] == "not validated"
    assert migrator.active_path() == "p1"


def test_commit_uses_new_path_congestion_window_and_rtt():
    migrator = build()
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.validate("p2", challenge["token"])
    assert migrator.commit("p2")["committed"] is True
    congestion = migrator.congestion()
    assert congestion["path_id"] == "p2"
    assert congestion["cwnd"] == 40.0
    assert congestion["rtt"] == 0.02


def test_amplification_budget_is_scoped_to_unvalidated_path():
    migrator = build()
    migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.record_received("p2", 100)
    assert migrator.send_budget("p2") == 300
    assert migrator.send("p2", 400) == 300
    assert migrator.send_budget("p2") == 0
    assert migrator.send("p2", 1) == 0


def test_stale_parallel_candidate_cannot_override_committed_path():
    migrator = build()
    migrator.add_path("p3", ("10.0.0.3", 4433), cwnd=30.0, rtt=0.03,
                      worker="w3")
    first = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    second = migrator.on_packet(packet("p3", ("10.0.0.3", 4433)))
    migrator.validate("p2", first["token"])
    migrator.validate("p3", second["token"])
    assert migrator.commit("p2")["committed"] is True
    late = migrator.commit("p3")
    assert late["committed"] is False
    assert late["reason"] == "stale candidate"
    assert migrator.active_path() == "p2"


def test_validation_timeout_returns_to_active_path():
    migrator = build()
    migrator.on_packet(packet("p2", ("10.0.0.2", 4433), now=10.0))
    result = migrator.check_timeout(16.0)
    assert result["rolled_back"] is True
    assert result["failed"] == ["p2"]
    assert result["active"] == "p1"
    assert migrator.commit("p2")["committed"] is False


def test_connection_ids_are_rerouted_to_new_worker():
    migrator = build()
    migrator.register_cid("cid-a", "p1")
    assert migrator.route("cid-a") == "w1"
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.validate("p2", challenge["token"])
    migrator.commit("p2")
    assert migrator.route("cid-a") == "w2"
    assert migrator.route("cid-unknown") == "w2"


def test_packet_number_space_is_per_path():
    migrator = build()
    assert migrator.on_packet(packet("p1", ("10.0.0.1", 4433), pn=100))["action"] == "accept"
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.validate("p2", challenge["token"])
    migrator.commit("p2")
    assert migrator.on_packet(packet("p2", ("10.0.0.2", 4433), pn=1))["action"] == "accept"


def test_preferred_address_offer_can_be_validated_and_used():
    migrator = build()
    offer = migrator.offer_preferred("p3", ("10.0.0.9", 4433),
                                     cwnd=25.0, rtt=0.04)
    assert offer["action"] == "challenge"
    assert migrator.active_path() == "p1"
    assert migrator.validate("p3", offer["token"])["validated"] is True
    assert migrator.commit("p3")["committed"] is True
    assert migrator.congestion()["cwnd"] == 25.0


def test_zero_rtt_payload_is_not_delivered_twice_after_migration():
    migrator = build()
    first = migrator.on_packet(packet("p1", ("10.0.0.1", 4433), pn=1,
                                      zero_rtt_id="z-1"))
    assert first["action"] == "accept"
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.validate("p2", challenge["token"])
    migrator.commit("p2")
    replay = migrator.on_packet(packet("p2", ("10.0.0.2", 4433), pn=2,
                                       zero_rtt_id="z-1"))
    assert replay["action"] == "replay_dropped"
    assert migrator.active_path() == "p2"


def test_disabled_migration_rejects_new_path():
    migrator = build(migration_enabled=False)
    result = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    assert result["action"] == "reject"
    assert result["reason"] == "migration disabled"
    assert migrator.active_path() == "p1"


def test_migration_state_survives_process_restart():
    migrator = build()
    migrator.register_cid("cid-a", "p1")
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    migrator.validate("p2", challenge["token"])
    migrator.commit("p2")
    state = migrator.export_state()
    restored = qpm.PathMigrator()
    restored.load_state(state)
    assert restored.active_path() == "p2"
    assert restored.route("cid-a") == "w2"
    assert restored.congestion()["rtt"] == 0.02


def test_validation_with_wrong_token_keeps_path_unvalidated():
    migrator = build()
    challenge = migrator.on_packet(packet("p2", ("10.0.0.2", 4433)))
    bad = migrator.validate("p2", "not-the-token")
    assert bad["validated"] is False
    assert migrator.path_state("p2") == "validating"
    good = migrator.validate("p2", challenge["token"])
    assert good["validated"] is True
