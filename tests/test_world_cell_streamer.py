import pytest

import world_cell_streamer as wcs


def streamer(load_radius=1):
    return wcs.WorldCellStreamer(cell_size=64.0, load_radius=load_radius, hysteresis=1)


def test_negative_coordinates_map_to_cells():
    s = streamer()
    assert s.cell_at(-65.0, 130.0) == (-2, 2)
    assert s.cell_at(0.0, 0.0) == (0, 0)


def test_hysteresis_prevents_churn_on_small_move():
    s = streamer()
    s.set_player((0.0, 0.0))
    s.update()
    resident = s.stats()["resident"]
    s.set_player((10.0, 0.0))
    s.update()
    assert s.stats()["resident"] == resident
    assert s.stats()["unloads"] == 0


def test_requests_are_sorted_by_distance():
    s = streamer(load_radius=2)
    s.set_player((0.0, 0.0))
    result = s.update()
    assert result["requests"][0] == (0, 0)
    assert result["requests"][-1] != (0, 0)


def test_heading_cone_gets_priority_at_equal_distance():
    s = streamer()
    s.set_player((0.0, 0.0), heading_deg=0.0)
    result = s.update()
    front = result["requests"].index((0, 1))
    behind = result["requests"].index((0, -1))
    assert front < behind


def test_refcount_keeps_cell_resident():
    s = streamer()
    s.request((5, 5), owner="a")
    s.request((5, 5), owner="b")
    s.release((5, 5), owner="a")
    s.update()
    assert (5, 5) in s.stats()["resident_cells"]


def test_refcount_zero_unloads_cell():
    s = streamer()
    s.request((5, 5), owner="a")
    s.update()
    s.release((5, 5), owner="a")
    s.update()
    assert (5, 5) not in s.stats()["resident_cells"]


def test_teleport_cancels_old_pending_requests():
    s = streamer(load_radius=3)
    s.set_player((0.0, 0.0))
    s.update()
    old_pending = s.stats()["loading"]
    s.teleport((10000.0, 10000.0))
    result = s.update()
    assert result["cancelled"] >= old_pending


def test_cross_cell_dependency_loads_whole_structure():
    s = streamer()
    s.add_dependency((0, 0), (3, 0))
    s.set_player((0.0, 0.0))
    result = s.update()
    assert (3, 0) in result["requests"]


def test_completion_callback_is_marshalled_to_main_thread():
    s = streamer()
    seen = []
    s.register_callback(lambda cell: seen.append(cell))
    s.request((2, 2), owner="task")
    s.complete((2, 2), thread="io")
    assert seen == []
    s.pump_main_thread()
    assert seen == [(2, 2)]


def test_tight_budget_still_loads_cells_under_player():
    s = streamer(load_radius=3)
    s.set_player((0.0, 0.0))
    result = s.update(budget_ms=0.0)
    assert set(result["requests"]) == {(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)}


def test_empty_cell_is_tracked_without_loading():
    s = streamer()
    s.mark_empty((5, 5))
    s.request((5, 5), owner="a")
    s.update()
    stats = s.stats()
    assert stats["empty_hits"] == 1
    assert (5, 5) not in stats["resident_cells"]


def test_duplicate_requests_are_merged():
    s = streamer()
    s.request((0, 0), owner="a")
    s.request((0, 0), owner="b")
    result = s.update()
    assert result["requests"].count((0, 0)) == 1
    assert s.stats()["ref_counts"][(0, 0)] == 2


def test_moving_far_unloads_old_cells():
    s = streamer()
    s.set_player((0.0, 0.0))
    s.update()
    s.set_player((1000.0, 1000.0))
    s.update()
    assert s.stats()["unloads"] >= 1


def test_boundary_cells_are_loaded_normally():
    s = streamer()
    s.set_player((-64.0, -64.0))
    result = s.update()
    assert (-2, -2) in result["requests"]


def test_update_reports_counts_and_elapsed():
    s = streamer()
    s.set_player((0.0, 0.0))
    result = s.update(budget_ms=2.0)
    assert result["elapsed_ms"] >= 0
    assert result["budget_ms"] == pytest.approx(2.0)
    assert result["loading"] == len(result["requests"])
