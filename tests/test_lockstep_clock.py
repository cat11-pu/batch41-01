import pytest

import lockstep_clock as lc


def test_advance_uses_fixed_logical_step():
    clock = lc.LockstepClock(step_ms=50)
    assert clock.advance(0) == []
    assert clock.advance(50) == [1]
    assert clock.advance(99) == []
    assert clock.advance(100) == [2]
    assert clock.advance(149) == []
    assert clock.current_frame() == 2


def test_catch_up_is_bounded_by_budget():
    clock = lc.LockstepClock(step_ms=10, max_catch_up=3)
    assert clock.advance(10) == [1]
    assert clock.catch_up(10) == [2, 3, 4]
    assert clock.catch_up(10) == [5, 6, 7]
    assert clock.catch_up(10) == [8, 9, 10]
    assert clock.catch_up(10) == []


def test_catch_up_never_skips_logical_frames():
    clock = lc.LockstepClock(step_ms=10, max_catch_up=2)
    assert clock.advance(10) == [1]
    first = clock.catch_up(5)
    second = clock.catch_up(5)
    third = clock.catch_up(5)
    assert first + second + third == [2, 3, 4, 5]


def test_pause_and_resume_are_atomic():
    clock = lc.LockstepClock(step_ms=50)
    assert clock.advance(100) == [1, 2]
    assert clock.pause(120) == 2
    assert clock.advance(500) == []
    assert clock.resume(500) == 2
    assert clock.advance(549) == []
    assert clock.advance(550) == [3]


def test_reconnect_returns_every_missing_frame_and_input():
    clock = lc.LockstepClock(step_ms=10)
    clock.subscribe("p1")
    assert clock.advance(50) == [1, 2, 3, 4, 5]
    clock.record_input(2, "p1", "jump")
    clock.record_input(4, "p1", "kick")
    result = clock.reconnect("p1", 1)
    assert result["frames"] == [2, 3, 4, 5]
    assert result["inputs"][2] == ["jump"]
    assert result["inputs"][4] == ["kick"]
    assert 3 not in result["inputs"]


def test_spectator_broadcast_is_independent_from_players():
    clock = lc.LockstepClock(step_ms=50)
    clock.subscribe("p1")
    first = clock.broadcast()
    clock.subscribe("spec", spectator=True)
    second = clock.broadcast()
    third = clock.broadcast()
    assert first["players"]["p1"]["seq"] == 1
    assert second["players"]["p1"]["seq"] == 2
    assert third["players"]["p1"]["seq"] == 3
    assert second["spectators"]["spec"]["seq"] == 1
    assert third["spectators"]["spec"]["seq"] == 2
    assert second["frame"] == 0


def test_unpack_sorts_merged_packets_by_sequence():
    clock = lc.LockstepClock()
    packets = [{"seq": 3, "frame": 3}, {"seq": 1, "frame": 1}, {"seq": 2, "frame": 2}]
    assert [packet["seq"] for packet in clock.unpack(packets)] == [1, 2, 3]


def test_unpack_rejects_sequence_gap_and_duplicate():
    clock = lc.LockstepClock()
    with pytest.raises(ValueError):
        clock.unpack([{"seq": 1, "frame": 1}, {"seq": 3, "frame": 3}])
    with pytest.raises(ValueError):
        clock.unpack([{"seq": 1, "frame": 1}, {"seq": 1, "frame": 1}])


def test_presentation_rate_is_per_client_only():
    clock = lc.LockstepClock(step_ms=50)
    clock.subscribe("low")
    clock.subscribe("normal")
    info = clock.set_presentation_rate("low", 10)
    assert info["fps"] == 10
    assert clock.presentation_rate("low")["fps"] == 10
    assert clock.presentation_rate("normal")["fps"] == 20
    assert clock.step_ms == 50


def test_client_time_cannot_advance_the_clock():
    clock = lc.LockstepClock(step_ms=50)
    assert clock.advance(50) == [1]
    assert clock.advance(50, client_ms=100000) == []
    assert clock.advance(100, client_ms=0) == [2]


def test_replay_records_logical_frames_in_order():
    clock = lc.LockstepClock(step_ms=10)
    assert clock.advance(30) == [1, 2, 3]
    clock.record_input(1, "p1", "a")
    clock.record_input(3, "p2", "b")
    frames = clock.replay()
    assert [entry["frame"] for entry in frames] == [1, 2, 3]
    assert frames[0]["inputs"] == [{"player": "p1", "action": "a"}]
    assert frames[1]["inputs"] == []
    assert frames[2]["inputs"] == [{"player": "p2", "action": "b"}]


def test_slow_client_does_not_block_broadcast_or_advance():
    clock = lc.LockstepClock(step_ms=10)
    clock.subscribe("fast")
    clock.subscribe("slow")
    assert clock.advance(50) == [1, 2, 3, 4, 5]
    for _ in range(5):
        packet = clock.broadcast()
        clock.ack("fast", packet["players"]["fast"]["seq"])
    assert clock.pending("fast") == 0
    assert clock.pending("slow") == 5
    assert clock.advance(100) == [6, 7, 8, 9, 10]


def test_frame_comparison_treats_numbers_as_unsigned():
    clock = lc.LockstepClock(frame_space=65536)
    assert clock.compare_frame(3, 65535) == 1
    assert clock.compare_frame(65535, 3) == -1
    assert clock.compare_frame(7, 7) == 0


def test_catch_up_handles_frame_number_wraparound():
    clock = lc.LockstepClock(step_ms=10, max_catch_up=8, frame_space=16)
    assert clock.advance(140) == list(range(1, 15))
    assert clock.current_frame() == 14
    assert clock.catch_up(2) == [15, 0, 1, 2]


def test_catch_up_to_current_or_past_frame_does_nothing():
    clock = lc.LockstepClock(step_ms=10)
    assert clock.advance(30) == [1, 2, 3]
    assert clock.catch_up(3) == []
    assert clock.catch_up(2) == []
    assert clock.current_frame() == 3
