import pytest

import bfd_session_fsm as bfd


def packet(state="down", discriminator=7, poll=False, final=False, echo=False,
           ttl=255, auth_key_id=None, min_tx=100, min_rx=100):
    return {
        "state": state,
        "discriminator": discriminator,
        "poll": poll,
        "final": final,
        "echo": echo,
        "ttl": ttl,
        "auth_key_id": auth_key_id,
        "min_tx": min_tx,
        "min_rx": min_rx,
    }


def fsm():
    return bfd.SessionFsm()


def opened(store=None, **config):
    store = store or fsm()
    args = {"desired_min_tx": 100, "required_min_rx": 100, "discriminator": 101}
    args.update(config)
    store.open("s1", args, 0)
    return store


def bring_up(store, start=0):
    store.recv("s1", packet(state="down"), start)
    store.recv("s1", packet(state="init"), start + 100)
    return store


def test_detect_multiplier_one_is_rejected():
    store = fsm()
    with pytest.raises(ValueError):
        store.open("s1", {"detect_mult": 1}, 0)


def test_open_reports_down_state_and_negotiated_intervals():
    store = fsm()
    result = store.open(
        "s1",
        {"desired_min_tx": 300, "required_min_rx": 200, "discriminator": 42},
        0,
    )
    assert result["state"] == "down"
    assert result["negotiated_tx"] == 300
    assert result["negotiated_rx"] == 200
    assert result["local_discriminator"] == 42
    with pytest.raises(ValueError):
        store.open("s1", {"discriminator": 43}, 0)


def test_receive_interval_below_negotiated_is_rejected():
    store = opened(required_min_rx=200, desired_min_tx=200)
    first = store.recv("s1", packet(state="down"), 0)
    too_fast = store.recv("s1", packet(state="down"), 50)
    later = store.recv("s1", packet(state="down"), 250)
    assert first["accepted"] is True
    assert too_fast["accepted"] is False
    assert too_fast["reason"] == "rx_too_fast"
    assert later["accepted"] is True


def test_down_init_up_progression():
    store = opened()
    first = store.recv("s1", packet(state="down"), 0)
    second = store.recv("s1", packet(state="init"), 100)
    assert first["state"] == "init"
    assert second["state"] == "up"
    assert store.state("s1")["remote_discriminator"] == 7


def test_admin_down_does_not_jump_to_init():
    store = opened()
    store.admin_down("s1", 0)
    result = store.recv("s1", packet(state="down"), 100)
    assert store.state("s1")["state"] == "down"
    assert result["state"] == "down"
    assert result["accepted"] is True


def test_admin_up_renegotiates_through_init():
    store = bring_up(opened())
    store.admin_down("s1", 200)
    result = store.admin_up("s1", 300)
    assert result["state"] == "init"
    assert store.state("s1")["state"] == "init"


def test_echo_packet_must_use_the_configured_interface():
    store = opened(echo_interface="eth0")
    wrong = store.recv("s1", packet(echo=True), 100, interface="eth1")
    right = store.recv("s1", packet(echo=True), 200, interface="eth0")
    assert wrong["accepted"] is False
    assert wrong["reason"] == "echo_interface"
    assert right["accepted"] is True
    assert store.state("s1")["state"] == "down"


def test_poll_packet_is_answered_with_final():
    store = bring_up(opened())
    response = store.recv("s1", packet(state="up", poll=True), 200)
    assert response["accepted"] is True
    assert response["final"] is True
    assert store.state("s1")["last_final"] is True


def test_auth_rotation_has_overlap_then_retires_old_key():
    store = opened()
    store.rotate_auth("s1", 1, 0)
    first = store.recv("s1", packet(state="down", auth_key_id=1), 1000)
    store.rotate_auth("s1", 2, 2000, overlap=30000)
    old_ok = store.recv("s1", packet(state="init", auth_key_id=1), 3000)
    new_ok = store.recv("s1", packet(state="up", auth_key_id=2), 4000)
    old_expired = store.recv("s1", packet(state="up", auth_key_id=1), 32001)
    assert first["accepted"] is True
    assert old_ok["accepted"] is True
    assert new_ok["accepted"] is True
    assert old_expired["accepted"] is False
    assert old_expired["reason"] == "auth"


def test_multi_hop_session_requires_ttl_of_250_or_more():
    store = opened(multi_hop=True)
    low = store.recv("s1", packet(state="down", ttl=1), 0)
    good = store.recv("s1", packet(state="down", ttl=255), 100)
    assert low["accepted"] is False
    assert low["reason"] == "ttl"
    assert good["accepted"] is True


def test_duplicate_local_discriminator_is_rejected():
    store = fsm()
    store.open("s1", {"discriminator": 1234}, 0)
    with pytest.raises(ValueError):
        store.open("s2", {"discriminator": 1234}, 0)


def test_flap_damping_is_time_based():
    store = opened(required_min_rx=1, desired_min_tx=1)

    def quick(state):
        return packet(state=state, min_tx=1, min_rx=1)

    store.recv("s1", quick("down"), 0)
    store.recv("s1", quick("init"), 50)
    for down_at in [200, 204, 208]:
        store.recv("s1", quick("down"), down_at)
        store.recv("s1", quick("down"), down_at + 1)
        store.recv("s1", quick("init"), down_at + 2)
    assert store.damped("s1", 208) is True
    assert store.damped("s1", 219) is False


def test_session_state_survives_flush_and_load(tmp_path):
    path = str(tmp_path / "bfd.json")
    store = bring_up(opened())
    store.flush(path)
    reborn = bfd.SessionFsm.load(path)
    info = reborn.state("s1")
    assert info["state"] == "up"
    assert info["remote_discriminator"] == 7


def test_unknown_auth_key_and_missing_key_are_rejected():
    store = opened()
    store.rotate_auth("s1", 1, 0)
    unknown = store.recv("s1", packet(state="down", auth_key_id=9), 1000)
    missing = store.recv("s1", packet(state="down"), 1100)
    known = store.recv("s1", packet(state="down", auth_key_id=1), 1200)
    assert unknown["accepted"] is False
    assert unknown["reason"] == "auth"
    assert missing["accepted"] is False
    assert known["accepted"] is True


def test_snapshot_counts_and_state_copy_isolation():
    store = fsm()
    store.open("s1", {"discriminator": 11}, 0)
    store.open("s2", {"discriminator": 12}, 0)
    store.recv("s1", packet(state="down"), 0)
    store.recv("s1", packet(state="init"), 100)
    store.admin_down("s2", 0)
    snapshot = store.snapshot()
    assert snapshot["sessions"] == 2
    assert snapshot["up"] == 1
    assert snapshot["admin_down"] == 1
    view = store.state("s1")
    view["state"] = "hacked"
    assert store.state("s1")["state"] == "up"
