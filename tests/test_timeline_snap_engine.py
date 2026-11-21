import pytest

import timeline_snap_engine as tse


def engine():
    e = tse.TimelineSnapEngine(fps_num=30, fps_den=1)
    e.add_track("v1", kind="video")
    e.add_track("sub", kind="subtitle", linked="v1")
    e.add_track("locked", kind="video", locked=True)
    return e


def test_snap_rounds_fractional_frame_to_grid():
    e = engine()
    low = e.snap("v1", 10.4, tolerance=0)
    high = e.snap("v1", 10.6, tolerance=0)
    assert low["frame"] == 10
    assert high["frame"] == 11
    assert low["elapsed_ms"] >= 0


def test_snap_uses_nearest_clip_edge():
    e = engine()
    e.add_clip("a", "v1", 0, 30)
    e.add_clip("b", "v1", 60, 30)
    result = e.snap("v1", 58, tolerance=3)
    assert result["point"] == 60
    assert result["frame"] == 60


def test_snap_leaves_frame_alone_outside_tolerance():
    e = engine()
    e.add_clip("a", "v1", 0, 30)
    result = e.snap("v1", 40, tolerance=3)
    assert result["point"] is None
    assert result["frame"] == 40


def test_snap_ignores_locked_track_edges():
    e = engine()
    e.add_clip("lock", "locked", 100, 20)
    result = e.snap("v1", 99, tolerance=3)
    assert result["point"] is None
    assert result["frame"] == 99


def test_move_rejects_negative_start():
    e = engine()
    e.add_clip("a", "v1", 10, 10)
    with pytest.raises(ValueError):
        e.move("a", -1)


def test_move_on_locked_track_is_rejected():
    e = engine()
    e.add_clip("l", "locked", 0, 10)
    with pytest.raises(ValueError):
        e.move("l", 20)


def test_ripple_delete_shifts_later_clips_on_track():
    e = engine()
    e.add_clip("a", "v1", 0, 10)
    e.add_clip("b", "v1", 10, 10)
    e.add_clip("c", "v1", 20, 10)
    e.ripple_delete("b")
    assert e.clip_state("a")["start"] == 0
    assert e.clip_state("c")["start"] == 10


def test_ripple_delete_shifts_linked_subtitle_track():
    e = engine()
    e.add_clip("b", "v1", 10, 10)
    e.add_clip("c", "v1", 20, 10)
    e.add_clip("s1", "sub", 10, 10)
    e.add_clip("s2", "sub", 20, 10)
    e.ripple_delete("b")
    assert e.clip_state("c")["start"] == 10
    assert e.clip_state("s1")["start"] == 0
    assert e.clip_state("s2")["start"] == 10


def test_transition_overlap_is_reported_as_transition():
    e = engine()
    e.add_clip("a", "v1", 0, 30, transition=4)
    e.add_clip("b", "v1", 30, 30, transition=4)
    intervals = e.overlap_intervals()
    matches = [i for i in intervals if {i["a"], i["b"]} == {"a", "b"}]
    assert len(matches) == 1
    assert matches[0]["kind"] == "transition"
    assert matches[0]["frames"] == 4


def test_hard_overlap_is_reported_as_conflict():
    e = engine()
    e.add_clip("a", "v1", 0, 30)
    e.add_clip("b", "v1", 28, 30)
    matches = [i for i in e.overlap_intervals() if {i["a"], i["b"]} == {"a", "b"}]
    assert len(matches) == 1
    assert matches[0]["kind"] == "conflict"
    assert matches[0]["frames"] == 2


def test_one_undo_restores_previous_start():
    e = engine()
    e.add_clip("a", "v1", 10, 10)
    e.move("a", 40)
    assert e.clip_state("a")["start"] == 40
    assert e.undo() is True
    assert e.clip_state("a")["start"] == 10
    assert e.undo() is False


def test_multi_select_move_preserves_relative_spacing():
    e = engine()
    e.add_clip("a", "v1", 10, 5)
    e.add_clip("b", "v1", 25, 5)
    e.move_many(["a", "b"], 30)
    assert e.clip_state("a")["start"] == 30
    assert e.clip_state("b")["start"] == 45


def test_collapsed_group_moves_in_timeline_coordinates():
    e = engine()
    e.add_clip("a", "v1", 10, 5)
    e.add_clip("b", "v1", 25, 5)
    e.add_group("g", ["a", "b"])
    e.collapse_group("g", True)
    e.move_group("g", 100)
    assert e.clip_state("a")["start"] == 100
    assert e.clip_state("b")["start"] == 115


def test_pixel_to_frame_conversion_is_exact_rational():
    e = engine()
    assert e.pixels_to_frames(1_000_000, 7, 3) == 12_857_143
    assert e.pixels_to_frames(100, 1000, 1) == 3


def test_edit_lock_rejects_background_mutation():
    e = engine()
    e.add_clip("a", "v1", 10, 10)
    e.begin_edit("drag")
    with pytest.raises(RuntimeError):
        e.move("a", 20, owner="transcode")
    e.move("a", 20, owner="drag")
    assert e.clip_state("a")["start"] == 20
    e.end_edit("drag")
