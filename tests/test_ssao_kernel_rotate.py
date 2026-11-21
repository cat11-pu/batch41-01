import math

import pytest

import ssao_kernel_rotate as ssao


def test_reconstruct_center_maps_to_view_axis():
    kernel = ssao.SsaoKernelRotate(100, 100, fov_deg=90.0)
    assert kernel.reconstruct(0.5, 0.5, 10.0) == pytest.approx((0.0, 0.0, -10.0))


def test_reconstruct_uses_fov_and_aspect():
    kernel = ssao.SsaoKernelRotate(200, 100, fov_deg=90.0)
    assert kernel.reconstruct(1.0, 0.5, 10.0) == pytest.approx((20.0, 0.0, -10.0))
    assert kernel.reconstruct(0.5, 1.0, 10.0) == pytest.approx((0.0, -10.0, -10.0))


def test_pixel_radius_shrinks_with_depth():
    kernel = ssao.SsaoKernelRotate(1920, 1080, fov_deg=60.0)
    near = kernel.pixel_radius(0.5, 5.0)
    far = kernel.pixel_radius(0.5, 10.0)
    assert near > 0.0
    assert near == pytest.approx(2.0 * far, rel=1e-6)


def test_rotate_kernel_by_ninety_degrees():
    kernel = ssao.SsaoKernelRotate(64, 64)
    assert kernel.rotate_kernel((1.0, 0.0, 0.0), 90.0) == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)
    assert kernel.rotate_kernel((0.0, 1.0, 0.0), 180.0) == pytest.approx((0.0, -1.0, 0.0), abs=1e-6)


def test_normal_from_flat_depth_points_forward():
    kernel = ssao.SsaoKernelRotate(64, 64)
    depth = [[5.0 for _ in range(5)] for _ in range(5)]
    assert kernel.normal_from_depth(depth, 2, 2) == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)


def test_normal_from_depth_uses_one_sided_at_silhouette():
    kernel = ssao.SsaoKernelRotate(64, 64)
    depth = [[1.0, 1.0, 9.0, 9.0, 9.0] for _ in range(5)]
    normal = kernel.normal_from_depth(depth, 1, 2)
    assert abs(normal[2]) > 0.9
    assert abs(normal[0]) < 0.2


def test_bilateral_upsample_does_not_bleed_background():
    kernel = ssao.SsaoKernelRotate(64, 64)
    ao_low = [[1.0, 0.0], [0.0, 0.0]]
    depth_low = [[1.0, 10.0], [10.0, 10.0]]
    depth_high = [[1.0 for _ in range(4)] for _ in range(4)]
    value = kernel.bilateral_upsample(ao_low, depth_low, depth_high, 2, 2)
    assert value > 0.8


def test_temporal_filter_rejects_depth_discontinuity():
    kernel = ssao.SsaoKernelRotate(64, 64)
    value = kernel.temporal_filter(0.2, 0.9, 6.0, 5.0, (0.0, 0.0, 1.0),
                                   (0.0, 0.0, 1.0), depth_threshold=0.1)
    assert value == pytest.approx(0.2)


def test_temporal_filter_rejects_normal_change():
    kernel = ssao.SsaoKernelRotate(64, 64)
    value = kernel.temporal_filter(0.2, 0.9, 5.0, 5.0, (1.0, 0.0, 0.0),
                                   (0.0, 0.0, 1.0), normal_threshold=0.9)
    assert value == pytest.approx(0.2)


def test_temporal_filter_blends_consistent_history():
    kernel = ssao.SsaoKernelRotate(64, 64)
    value = kernel.temporal_filter(0.2, 0.9, 5.0, 5.0, (0.0, 0.0, 1.0),
                                   (0.0, 0.0, 1.0))
    assert 0.2 <= value <= 0.9
    assert value > 0.5


def test_compute_flat_surface_is_unoccluded():
    kernel = ssao.SsaoKernelRotate(4, 4, samples=8)
    depth = [[5.0 for _ in range(4)] for _ in range(4)]
    result = kernel.compute(depth)
    assert result["samples"] == 8
    assert len(result["ao"]) == 4
    for row in result["ao"]:
        assert len(row) == 4
        for value in row:
            assert math.isfinite(value)
            assert value > 0.9


def test_strength_zero_short_circuits_sampling():
    kernel = ssao.SsaoKernelRotate(4, 4, samples=8, strength=0.0)
    depth = [[5.0 for _ in range(4)] for _ in range(4)]
    result = kernel.compute(depth)
    assert result["samples"] == 0
    for row in result["ao"]:
        for value in row:
            assert value == pytest.approx(1.0)


def test_sky_and_invalid_depth_stay_finite():
    kernel = ssao.SsaoKernelRotate(4, 4, samples=8)
    depth = [
        [-1.0, 1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0, 0.0],
    ]
    result = kernel.compute(depth)
    for row in result["ao"]:
        for value in row:
            assert math.isfinite(value)
    assert result["ao"][0][3] == pytest.approx(1.0)
    assert result["ao"][3][3] == pytest.approx(1.0)


def test_over_budget_reduces_samples_and_reports():
    kernel = ssao.SsaoKernelRotate(4, 4, samples=8, budget_ms=4.0)
    depth = [[5.0 for _ in range(4)] for _ in range(4)]
    result = kernel.compute(depth, elapsed_ms=9.0)
    assert result["degraded"] is True
    assert 1 <= result["samples"] < 8
    assert result["notes"]


def test_viewports_do_not_share_history():
    small = ssao.SsaoKernelRotate(8, 8, samples=8)
    large = ssao.SsaoKernelRotate(16, 16, samples=8)
    small_result = small.compute([[5.0 for _ in range(8)] for _ in range(8)])
    snapshot = [list(row) for row in small_result["ao"]]
    large_result = large.compute([[5.0 for _ in range(16)] for _ in range(16)])
    assert len(small_result["ao"]) == 8
    assert len(large_result["ao"]) == 16
    assert len(large_result["ao"][0]) == 16
    assert small.width == 8
    assert large.width == 16
    assert small_result["ao"] == snapshot
