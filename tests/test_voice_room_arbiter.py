import threading

import pytest

import voice_room_arbiter as vr


def room_with(accounts):
    arbiter = vr.VoiceRoomArbiter()
    room_id = arbiter.create_room("team-1", "s1")
    for account in accounts:
        arbiter.join(room_id, account)
    return arbiter, room_id


def test_second_device_join_is_rejected():
    arbiter, room_id = room_with(["host"])
    with pytest.raises(ValueError):
        arbiter.join(room_id, "host", device="phone")
    assert list(arbiter.room_state(room_id)["members"]) == ["host"]


def test_join_assigns_lowest_free_seat():
    arbiter, room_id = room_with(["host", "second"])
    assert arbiter.room_state(room_id)["members"]["second"]["seat"] == 1
    arbiter.leave(room_id, "host")
    arbiter.join(room_id, "third")
    assert arbiter.room_state(room_id)["members"]["third"]["seat"] == 0


def test_kicked_account_cannot_rejoin():
    arbiter, room_id = room_with(["host", "guest"])
    arbiter.kick(room_id, "host", "guest")
    assert "guest" not in arbiter.room_state(room_id)["members"]
    with pytest.raises(PermissionError):
        arbiter.join(room_id, "guest")
    with pytest.raises(PermissionError):
        arbiter.reconnect(room_id, "guest")


def test_kick_requires_host():
    arbiter, room_id = room_with(["host", "guest"])
    with pytest.raises(PermissionError):
        arbiter.kick(room_id, "guest", "host")
    assert "host" in arbiter.room_state(room_id)["members"]


def test_host_transfer_skips_offline_member():
    arbiter, room_id = room_with(["host", "guest"])
    arbiter.set_online(room_id, "guest", False)
    with pytest.raises(ValueError):
        arbiter.transfer(room_id, "host", "guest")
    assert arbiter.room_state(room_id)["host"] == "host"


def test_host_transfer_to_online_member():
    arbiter, room_id = room_with(["host", "guest"])
    result = arbiter.transfer(room_id, "host", "guest")
    assert result["host"] == "guest"
    assert arbiter.room_state(room_id)["host"] == "guest"


def test_mute_is_server_authoritative():
    arbiter, room_id = room_with(["host", "guest"])
    assert "guest" in arbiter.audible(room_id, "host")
    arbiter.mute(room_id, "guest", True)
    assert "guest" not in arbiter.audible(room_id, "host")
    assert arbiter.room_state(room_id)["members"]["guest"]["muted"] is True


def test_unmute_restores_audibility():
    arbiter, room_id = room_with(["host", "guest"])
    arbiter.mute(room_id, "guest", True)
    arbiter.mute(room_id, "guest", False)
    assert "guest" in arbiter.audible(room_id, "host")


def test_room_ids_are_unique_across_servers():
    arbiter = vr.VoiceRoomArbiter()
    first = arbiter.create_room("team-1", "s1")
    second = arbiter.create_room("team-1", "s2")
    assert first != second
    arbiter.join(first, "alpha")
    arbiter.join(second, "beta")
    assert "beta" not in arbiter.room_state(first)["members"]
    assert "alpha" not in arbiter.room_state(second)["members"]


def test_reconnect_restores_seat_and_mute():
    arbiter, room_id = room_with(["host", "guest"])
    arbiter.mute(room_id, "guest", True)
    arbiter.set_online(room_id, "guest", False)
    result = arbiter.reconnect(room_id, "guest", device="phone")
    assert result["seat"] == 1
    assert result["muted"] is True


def test_capacity_is_atomic_under_concurrency():
    arbiter = vr.VoiceRoomArbiter()
    room_id = arbiter.create_room("team-1", "s1")
    for index in range(7):
        arbiter.join(room_id, "filled-%d" % index)
    barrier = threading.Barrier(6)
    outcomes = []

    def worker(index):
        barrier.wait()
        try:
            arbiter.join(room_id, "rush-%d" % index)
            outcomes.append("ok")
        except ValueError:
            outcomes.append("full")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert len(arbiter.room_state(room_id)["members"]) == 8


def test_channel_switch_leaves_old_channel():
    arbiter, room_id = room_with(["host", "guest"])
    arbiter.switch_channel(room_id, "guest", "raid")
    assert "guest" not in arbiter.audible(room_id, "host")
    arbiter.switch_channel(room_id, "guest", "team")
    assert "guest" in arbiter.audible(room_id, "host")


def test_blocked_speaker_is_filtered():
    arbiter, room_id = room_with(["host", "guest", "other"])
    arbiter.block("host", "guest")
    assert "guest" not in arbiter.audible(room_id, "host")
    assert "other" in arbiter.audible(room_id, "host")
    assert "guest" in arbiter.audible(room_id, "other")


def test_close_releases_room_resources():
    arbiter, room_id = room_with(["host", "guest"])
    result = arbiter.close(room_id)
    assert result["closed"] is True
    assert arbiter.stats()["resources"] == 0
    with pytest.raises(ValueError):
        arbiter.join(room_id, "late")


def test_stats_track_open_and_closed_rooms():
    arbiter = vr.VoiceRoomArbiter()
    first = arbiter.create_room("team-1", "s1")
    arbiter.create_room("team-2", "s1")
    assert arbiter.stats()["rooms_open"] == 2
    arbiter.close(first)
    stats = arbiter.stats()
    assert stats["rooms_open"] == 1
    assert stats["rooms_closed"] == 1
