import pytest

import multiview_compositor as mvc


def comp():
    return mvc.MultiviewCompositor(width=1920, height=1080, budget_ms=8.0)


def layout_two():
    return [
        {"source": "a", "x": 0, "y": 0, "w": 960, "h": 1080},
        {"source": "b", "x": 960, "y": 0, "w": 960, "h": 1080},
    ]


def test_pts_is_converted_to_master_milliseconds():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 30, timebase=(30, 1))
    frame = c.select_frame("a", 1000)
    assert frame["pts_ms"] == pytest.approx(1000.0)


def test_nearest_buffered_frame_is_selected():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 100, timebase=(1000, 1), duration_ms=100)
    assert c.select_frame("a", 40)["pts_ms"] == 0
    assert c.select_frame("a", 90)["pts_ms"] == 100


def test_vfr_uses_real_frame_duration():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=40)
    c.push_frame("a", 40, timebase=(1000, 1), duration_ms=500)
    assert c.select_frame("a", 100)["pts_ms"] == 40


def test_dropped_frames_do_not_compress_later_timestamps():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 100, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 500, timebase=(1000, 1), duration_ms=100)
    stale = c.select_frame("a", 300)
    assert stale["pts_ms"] == 100
    assert stale["stale_ms"] == 200
    assert c.select_frame("a", 500)["pts_ms"] == 500


def test_layout_switch_preallocates_targets():
    c = comp()
    result = c.set_layout(layout_two())
    assert result["preallocated"] is True
    assert result["cells"] == 2


def test_scaling_uses_bilinear_by_default():
    c = comp()
    c.set_layout(layout_two())
    assert c.stats()["scale"] == "bilinear"


def test_composition_order_follows_layout_not_arrival():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("b", 0, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    result = c.compose(0)
    assert [layer["source"] for layer in result["layers"]] == ["a", "b"]


def test_missing_source_gets_placeholder():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    result = c.compose(0)
    assert "b" in result["placeholders"]
    assert result["placeholders"]["b"]["state"] == "offline"


def test_hdr_source_is_converted_to_sdr():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100, transfer="pq")
    c.push_frame("b", 0, timebase=(1000, 1), duration_ms=100, transfer="bt709")
    result = c.compose(0)
    layer_a = [l for l in result["layers"] if l["source"] == "a"][0]
    assert layer_a["color_converted"] is True
    assert layer_a["color_space"] == "sdr"


def test_zero_width_cell_is_rejected():
    c = comp()
    with pytest.raises(ValueError):
        c.set_layout([{"source": "a", "x": 0, "y": 0, "w": 0, "h": 100}])


def test_cell_outside_canvas_is_rejected():
    c = comp()
    with pytest.raises(ValueError):
        c.set_layout([{"source": "a", "x": 1800, "y": 0, "w": 200, "h": 100}])


def test_over_budget_composition_degrades_resolution():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    result = c.compose(0, budget_ms=0.0)
    assert result["degraded"] is True
    assert result["width"] < 1920
    assert result["height"] < 1080


def test_audio_tracks_are_aligned_to_master_clock():
    c = comp()
    c.set_layout(layout_two())
    tracks = [
        {"id": "mic", "pts_ms": 500},
        {"id": "line", "pts_ms": 480},
    ]
    result = c.align_audio(520, tracks)
    assert result["offsets"]["mic"] == 20
    assert result["offsets"]["line"] == 40


def test_frame_queue_reports_atomic_swaps():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 0, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 40, timebase=(1000, 1), duration_ms=100)
    c.push_frame("a", 80, timebase=(1000, 1), duration_ms=100)
    stats = c.stats()
    assert stats["queued"] == 3
    assert stats["queue_swaps"] == 3


def test_compose_reports_end_to_end_latency():
    c = comp()
    c.set_layout(layout_two())
    c.push_frame("a", 900, timebase=(1000, 1), duration_ms=200)
    c.push_frame("b", 900, timebase=(1000, 1), duration_ms=200)
    result = c.compose(1000)
    assert result["latency_ms"] == pytest.approx(100.0)
    assert result["dropped_frames"] == 0
    assert result["elapsed_ms"] >= 0
