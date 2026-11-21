import pytest

import room_seat_ledger as rsl


def test_duplicate_join_same_player_keeps_single_seat():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    first = ledger.join(room, "alice", connection_id="dev-a", now=1.0)
    second = ledger.join(room, "alice", connection_id="dev-b", now=2.0)
    assert first["seat"] == second["seat"]
    assert len(ledger.members(room)) == 2
    assert ledger.connection_of(room, "alice") == "dev-b"


def test_spectator_cannot_ready_and_other_flags_stay():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    ledger.join(room, "p1", team="a", now=1.0)
    ledger.join(room, "spec", spectator=True, now=2.0)
    ledger.set_ready(room, "p1", True, now=3.0)
    with pytest.raises(ValueError):
        ledger.set_ready(room, "spec", True, now=4.0)
    state = ledger.ready_state(room)
    assert state["p1"] is True
    assert state["host"] is False
    assert "spec" not in state


def test_host_transfer_skips_spectator_and_follows_join_order():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host", now=0.0)
    ledger.join(room, "spec", spectator=True, now=1.0)
    ledger.join(room, "p1", now=2.0)
    ledger.join(room, "p2", now=3.0)
    result = ledger.leave(room, "host", now=4.0)
    assert result["was_host"] is True
    assert result["new_host"] == "p1"


def test_team_switch_updates_server_counts_and_clears_ready():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    ledger.join(room, "p1", team="a", now=1.0)
    ledger.join(room, "p2", team="b", now=2.0)
    ledger.set_ready(room, "p1", True, now=3.0)
    result = ledger.change_team(room, "p1", "b", now=4.0)
    assert result == {"a": 1, "b": 2}
    assert ledger.team_counts(room) == {"a": 1, "b": 2}
    assert ledger.ready_state(room)["p1"] is False


def test_dissolved_room_leaves_lobby_and_rejects_join():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    assert room in ledger.active_rooms()
    assert ledger.dissolve(room, now=1.0) is True
    assert room not in ledger.active_rooms()
    with pytest.raises(KeyError):
        ledger.join(room, "late", now=2.0)


def test_reconnect_resolves_by_identity_not_seat():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    joined = ledger.join(room, "p1", connection_id="conn-1", now=1.0)
    ledger.join(room, "p2", connection_id="conn-2", now=2.0)
    result = ledger.reconnect(room, "p1", "conn-3", now=3.0)
    assert result["seat"] == joined["seat"]
    assert ledger.connection_of(room, "p1") == "conn-3"
    assert len(ledger.members(room)) == 3
    with pytest.raises(KeyError):
        ledger.reconnect(room, "ghost", "conn-4", now=4.0)


def test_invite_clicked_twice_by_same_account_stays_one_member():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    first = ledger.join_with_invite(room, "alice", "tok-1", connection_id="a", now=1.0)
    second = ledger.join_with_invite(room, "alice", "tok-1", connection_id="b", now=2.0)
    assert first["seat"] == second["seat"]
    assert len(ledger.members(room)) == 2
    assert ledger.connection_of(room, "alice") == "b"
    with pytest.raises(ValueError):
        ledger.join_with_invite(room, "bob", "", connection_id="c", now=3.0)


def test_countdown_is_cancelled_when_roster_changes():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    ledger.join(room, "p1", team="b", now=1.0)
    ledger.set_ready(room, "host", True, now=2.0)
    ledger.set_ready(room, "p1", True, now=3.0)
    started = ledger.start_countdown(room, 10, now=4.0)
    assert started["ends_at"] == 14.0
    assert ledger.tick(room, 9.0) == "pending"
    ledger.leave(room, "p1", now=13.0)
    assert ledger.tick(room, 13.5) == "cancelled"
    assert ledger.tick(room, 30.0) == "cancelled"
    with pytest.raises(ValueError):
        ledger.start_countdown(room, 10, now=31.0)


def test_room_ids_are_scoped_by_region():
    regional = rsl.SeatLedger("r1")
    first = regional.create_room("host")
    third = regional.create_room("host")
    second = rsl.SeatLedger("r2").create_room("host")
    assert first == "r1:000001"
    assert second == "r2:000001"
    assert third == "r1:000002"


def test_seat_lock_blocks_until_ttl_expires():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    assert ledger.lock_seat(room, 3, "holder", now=0.0, ttl=10.0) is True
    with pytest.raises(ValueError):
        ledger.join(room, "p1", seat=3, now=5.0)
    assert ledger.release_expired_locks(room, now=9.0) == []
    joined = ledger.join(room, "p1", seat=3, now=10.0)
    assert joined["seat"] == 3


def test_ready_state_is_kept_on_server_across_reconnect():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    ledger.join(room, "p1", connection_id="old", now=1.0)
    ledger.set_ready(room, "p1", True, now=2.0)
    ledger.reconnect(room, "p1", "new", now=3.0)
    assert ledger.ready_state(room)["p1"] is True
    info = ledger.set_ready(room, "p1", False, now=4.0)
    assert info["ready"] is False
    assert ledger.ready_state(room)["p1"] is False


def test_seat_occupancy_is_unique_and_auto_picks_free_seat():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    first = ledger.join(room, "p1", seat=2, now=1.0)
    second = ledger.join(room, "p2", now=2.0)
    assert first["seat"] == 2
    assert second["seat"] == 3
    with pytest.raises(ValueError):
        ledger.join(room, "p3", seat=2, now=3.0)
    with pytest.raises(ValueError):
        ledger.join(room, "p4", seat=99, now=4.0)


def test_countdown_requires_even_teams_and_all_ready():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    ledger.join(room, "p1", team="a", now=1.0)
    ledger.join(room, "p2", team="b", now=2.0)
    for player in ("host", "p1", "p2"):
        ledger.set_ready(room, player, True, now=3.0)
    with pytest.raises(ValueError):
        ledger.start_countdown(room, 10, now=4.0)
    ledger.join(room, "p3", team="b", now=5.0)
    ledger.set_ready(room, "p3", True, now=6.0)
    started = ledger.start_countdown(room, 10, now=7.0)
    assert started["ends_at"] == 17.0


def test_leave_is_reentrant_and_rejoin_gets_a_seat():
    ledger = rsl.SeatLedger("r1")
    room = ledger.create_room("host")
    joined = ledger.join(room, "p1", now=1.0)
    first = ledger.leave(room, "p1", now=2.0)
    second = ledger.leave(room, "p1", now=3.0)
    again = ledger.join(room, "p1", now=4.0)
    assert first["left"] is True
    assert second["left"] is False
    assert again["seat"] == joined["seat"]
    assert len(ledger.members(room)) == 2


def test_capacity_is_enforced_on_join():
    ledger = rsl.SeatLedger("r1", capacity=3)
    room = ledger.create_room("host", now=0.0)
    ledger.join(room, "p1", now=1.0)
    ledger.join(room, "p2", now=2.0)
    with pytest.raises(ValueError):
        ledger.join(room, "p3", now=3.0)
