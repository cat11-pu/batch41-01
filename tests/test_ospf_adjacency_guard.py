import pytest

import ospf_adjacency_guard as ospf


def guard(**kwargs):
    return ospf.AdjacencyGuard(**kwargs)


def hello(**overrides):
    packet = {
        "router_id": "2.2.2.2",
        "area_id": "0.0.0.0",
        "interface": "eth0",
        "source": "10.0.0.5",
        "auth_len": 16,
        "auth": "s3cret",
        "priority": 10,
    }
    packet.update(overrides)
    return packet


def ready(**kwargs):
    instance = guard(**kwargs)
    result = instance.on_hello(hello())
    assert result["accepted"] is True
    return instance


def dd(**overrides):
    packet = {"router_id": "2.2.2.2", "area_id": "0.0.0.0", "seq": 1,
              "lsa_count": 1}
    packet.update(overrides)
    return packet


def lsa(**overrides):
    packet = {
        "type": 1,
        "lsa_id": "10.0.0.0",
        "adv_router": "2.2.2.2",
        "checksum": 12345,
        "age": 100,
        "seq": 20,
        "area_id": "0.0.0.0",
        "prefix": "10.0.0.0/24",
        "metric": 100,
        "origin": "internal",
        "from_area": "0.0.0.0",
    }
    packet.update(overrides)
    return packet


def test_hello_with_wrong_auth_length_is_rejected_and_audited():
    instance = guard()
    result = instance.on_hello(hello(auth_len=8))
    assert result["accepted"] is False
    assert result["reason"] == "bad auth length"
    assert instance.snapshot()["neighbors"] == {}
    assert any(entry["reason"] == "bad auth length"
               for entry in instance.snapshot()["audit"])


def test_hello_area_and_subnet_mismatch_are_rejected():
    instance = guard()
    wrong_area = instance.on_hello(hello(area_id="0.0.0.1"))
    assert wrong_area["reason"] == "area mismatch"
    wrong_subnet = instance.on_hello(hello(source="10.0.1.5"))
    assert wrong_subnet["reason"] == "subnet mismatch"
    assert instance.snapshot()["neighbors"] == {}


def test_valid_hello_creates_neighbor_in_init_state():
    instance = guard()
    result = instance.on_hello(hello(router_id="3.3.3.3"))
    assert result["accepted"] is True
    assert result["state"] == "Init"
    assert instance.snapshot()["neighbors"] == {"3.3.3.3": "Init"}


def test_duplicate_dd_sequence_is_rejected():
    instance = ready()
    assert instance.on_dd(dd(seq=900))["accepted"] is True
    replay = instance.on_dd(dd(seq=900))
    assert replay["accepted"] is False
    assert replay["reason"] == "dd sequence replay"


def test_dd_sequence_wrap_is_rejected():
    instance = ready()
    assert instance.on_dd(dd(seq=0xFFFFFFF0))["accepted"] is True
    wrapped = instance.on_dd(dd(seq=5))
    assert wrapped["accepted"] is False
    assert wrapped["reason"] == "dd sequence wrapped"


def test_lsa_with_zero_checksum_is_not_installed():
    instance = ready()
    result = instance.on_lsa(lsa(checksum=0))
    assert result["accepted"] is False
    assert result["reason"] == "invalid lsa checksum"
    assert instance.snapshot()["lsdb"] == {}


def test_lsa_age_at_max_age_is_rejected():
    instance = ready()
    result = instance.on_lsa(lsa(age=3600))
    assert result["accepted"] is False
    assert result["reason"] == "lsa age out of range"
    assert instance.snapshot()["lsdb"] == {}


def test_external_type5_lsa_is_filtered_by_policy():
    closed = ready()
    rejected = closed.on_lsa(lsa(type=5, origin="asbr"))
    assert rejected["accepted"] is False
    assert rejected["reason"] == "external lsa filtered"
    opened = ready(allow_external=True)
    accepted = opened.on_lsa(lsa(type=5, origin="asbr"))
    assert accepted["accepted"] is True


def test_abr_type3_reinjection_into_backbone_is_rejected():
    instance = ready()
    rejected = instance.on_lsa(lsa(type=3, origin="abr", from_area="0.0.0.1"))
    assert rejected["accepted"] is False
    assert rejected["reason"] == "abr reinjection"
    accepted = instance.on_lsa(lsa(type=3, origin="abr", from_area="0.0.0.0"))
    assert accepted["accepted"] is True


def test_dr_tie_is_broken_by_router_id():
    instance = guard()
    result = instance.elect_dr([
        {"router_id": "1.1.1.1", "priority": 100, "trusted": True},
        {"router_id": "2.2.2.2", "priority": 100, "trusted": True},
    ])
    assert result["dr"] == "2.2.2.2"
    assert result["backup"] == "1.1.1.1"


def test_untrusted_high_priority_candidate_cannot_win_dr():
    instance = guard()
    result = instance.elect_dr([
        {"router_id": "2.2.2.2", "priority": 100, "trusted": True},
        {"router_id": "3.3.3.3", "priority": 255, "trusted": False},
    ])
    assert result["dr"] == "2.2.2.2"
    assert any(entry["reason"] == "untrusted dr candidate"
               for entry in instance.snapshot()["audit"])


def test_state_machine_forbids_down_to_full_jump():
    instance = guard()
    instance.register_neighbor("5.5.5.5")
    illegal = instance.transition("5.5.5.5", "Full")
    assert illegal["ok"] is False
    assert illegal["reason"] == "illegal transition"
    assert instance.snapshot()["neighbors"]["5.5.5.5"] == "Down"
    legal = instance.transition("5.5.5.5", "Init")
    assert legal["ok"] is True


def test_lsr_retransmit_limit_rejects_flood():
    instance = ready(max_retransmit=2)
    first = instance.on_lsr({"router_id": "2.2.2.2", "lsa_id": "10.0.0.0"})
    second = instance.on_lsr({"router_id": "2.2.2.2", "lsa_id": "10.0.0.0"})
    third = instance.on_lsr({"router_id": "2.2.2.2", "lsa_id": "10.0.0.0"})
    assert first["accepted"] is True
    assert second["accepted"] is True
    assert third["accepted"] is False
    assert third["reason"] == "retransmit limit exceeded"


def test_stale_lsa_cannot_overwrite_newer_entry():
    instance = ready()
    assert instance.on_lsa(lsa(seq=20, metric=100, checksum=500))["accepted"] is True
    stale = instance.on_lsa(lsa(seq=10, metric=5, checksum=900))
    assert stale["accepted"] is False
    assert stale["reason"] == "stale lsa"
    assert instance.lsdb_entry(1, "10.0.0.0", "2.2.2.2")["metric"] == 100
    assert instance.on_lsa(lsa(seq=21, metric=5, checksum=100))["accepted"] is True
    assert instance.lsdb_entry(1, "10.0.0.0", "2.2.2.2")["metric"] == 5


def test_restart_restore_rejects_expired_lsa_and_keeps_previous_lsdb():
    instance = ready()
    assert instance.on_lsa(lsa())["accepted"] is True
    digest = instance.lsdb_digest()
    expired = instance.restore_lsdb([lsa(age=7200)])
    assert expired["restored"] is False
    assert expired["reason"] == "lsa age out of range"
    assert instance.lsdb_digest() == digest
    restored = instance.restore_lsdb([lsa()], expected_digest=digest)
    assert restored["restored"] is True
    assert restored["count"] == 1
