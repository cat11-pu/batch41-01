import pytest

import gyro_stabilize as gs


def stabilizer(width=1920, height=1080, fps=30):
    return gs.GyroStabilize(width=width, height=height, fps=fps)


def test_calibration_aligns_frame_and_gyro_times():
    g = stabilizer()
    gyro = [{"t_ms": t, "gx": 0.0, "gy": 0.0, "gz": 0.0} for t in range(0, 40, 10)]
    result = g.calibrate([5, 15, 25], gyro)
    assert result["aligned"] == 3
    assert abs(result["offset_ms"]) <= 5.0


def test_rolling_shutter_uses_row_readout_time():
    plan = stabilizer(fps=30).plan(0)
    assert plan["row_readout_ms"] == pytest.approx(1000.0 / 30.0 * 0.5, abs=0.1)


def test_crop_ratio_adapts_to_shake_magnitude():
    g = stabilizer()
    small = g.plan(0, shake_deg=0.5)
    large = g.plan(0, shake_deg=5.0)
    assert large["crop_ratio"] < small["crop_ratio"] <= 1.0


def test_crop_rect_never_exposes_black_edges():
    plan = stabilizer().plan(0, shake_deg=8.0)
    x, y, w, h = plan["crop_rect"]
    assert x >= 0.0
    assert y >= 0.0
    assert x + w <= 1.0 + 1e-9
    assert y + h <= 1.0 + 1e-9


def test_horizon_uses_low_pass_gravity():
    plan = stabilizer().plan(0, shake_deg=30.0)
    assert abs(plan["horizon_deg"]) <= 5.0


def test_smoothing_window_is_time_based():
    plan30 = stabilizer(fps=30).plan(0)
    plan60 = stabilizer(fps=60).plan(0)
    assert plan30["smooth_window_ms"] == pytest.approx(500.0)
    assert plan60["smooth_window_ms"] == pytest.approx(500.0)


def test_missing_gyro_is_extrapolated_from_angular_velocity():
    g = stabilizer()
    g.add_gyro(0, 0.0, 0.0, 10.0, seq=1)
    g.add_gyro(10, 0.0, 0.0, 10.0, seq=2)
    rotation = g.estimate_rotation(20)
    assert rotation[2] > 0.0


def test_distortion_correction_runs_before_stabilization():
    plan = stabilizer().plan(0)
    assert plan["stage_order"] == ["distortion", "stabilize"]


def test_output_dimensions_are_forced_even():
    plan = stabilizer(width=1921, height=1081).plan(0)
    width, height = plan["output_size"]
    assert width % 2 == 0
    assert height % 2 == 0
    assert width <= 1921
    assert height <= 1081


def test_bias_estimate_averages_static_samples():
    g = stabilizer()
    samples = [{"gx": 0.0, "gy": 0.0, "gz": 0.5} for _ in range(8)]
    assert g.estimate_bias(samples) == pytest.approx(0.5, abs=1e-6)


def test_ring_buffer_counts_sequence_gaps():
    g = stabilizer()
    g.add_gyro(0, 0.0, 0.0, 0.0, seq=1)
    g.add_gyro(10, 0.0, 0.0, 0.0, seq=2)
    g.add_gyro(20, 0.0, 0.0, 0.0, seq=5)
    g.add_gyro(30, 0.0, 0.0, 0.0, seq=2)
    stats = g.stats()
    assert stats["gaps"] == 1
    assert stats["samples"] == 3


def test_zero_budget_drops_to_reduced_precision():
    plan = stabilizer().plan(0, budget_ms=0.0)
    assert plan["precision"] == "reduced"


def test_invalid_geometry_and_fps_are_rejected():
    with pytest.raises(ValueError):
        gs.GyroStabilize(width=0, height=1080, fps=30)
    with pytest.raises(ValueError):
        gs.GyroStabilize(width=1920, height=1080, fps=0)


def test_crop_ratio_stays_within_bounds_under_extreme_shake():
    plan = stabilizer().plan(0, shake_deg=100.0)
    assert 0.0 < plan["crop_ratio"] <= 1.0


def test_plan_reports_matrix_and_elapsed():
    plan = stabilizer().plan(0)
    assert len(plan["matrix"]) == 9
    assert plan["elapsed_ms"] >= 0
