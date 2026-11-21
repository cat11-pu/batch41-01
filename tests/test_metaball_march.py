import math
import threading

import pytest

import metaball_march as mm

BOUNDS = ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))


def single_ball(iso=0.5, radius=1.0):
    march = mm.MetaballMarch(iso=iso)
    march.add_ball((0.0, 0.0, 0.0), radius)
    return march


def test_field_single_ball():
    march = single_ball()
    assert march.field((0.0, 0.0, 0.0)) == pytest.approx(1.0)
    assert march.field((0.5, 0.0, 0.0)) == pytest.approx(0.75)
    assert march.field((1.0, 0.0, 0.0)) == pytest.approx(0.0)
    assert march.field((2.0, 0.0, 0.0)) == 0.0


def test_field_sums_overlapping_balls():
    march = mm.MetaballMarch(iso=0.5)
    march.add_ball((0.0, 0.0, 0.0), 1.0)
    march.add_ball((1.0, 0.0, 0.0), 1.0)
    assert march.field((0.5, 0.0, 0.0)) == pytest.approx(1.5)


def test_negative_or_zero_radius_is_rejected():
    march = mm.MetaballMarch()
    with pytest.raises(ValueError):
        march.add_ball((0.0, 0.0, 0.0), -1.0)
    with pytest.raises(ValueError):
        march.add_ball((0.0, 0.0, 0.0), 0.0)


def test_non_finite_ball_is_rejected():
    march = mm.MetaballMarch()
    with pytest.raises(ValueError):
        march.add_ball((0.0, 0.0, 0.0), float("inf"))
    with pytest.raises(ValueError):
        march.add_ball((float("nan"), 0.0, 0.0), 1.0)


def test_threshold_on_surface_still_meshes():
    march = single_ball(iso=0.0)
    result = march.polygonize(*BOUNDS)
    assert len(result["vertices"]) > 0
    assert len(result["indices"]) > 0


def test_normals_are_unit_length():
    march = single_ball()
    result = march.polygonize(*BOUNDS)
    for normal in result["normals"]:
        length = math.sqrt(sum(component * component for component in normal))
        assert length == pytest.approx(1.0, abs=1e-3)


def test_normals_point_outward_for_small_radius():
    march = single_ball(radius=0.25)
    result = march.polygonize((-0.3, -0.3, -0.3), (0.3, 0.3, 0.3))
    vertices = result["vertices"]
    normals = result["normals"]
    best = max(range(len(vertices)), key=lambda index: vertices[index][0])
    assert vertices[best][0] > 0.1
    assert normals[best][0] > 0.7


def test_vertices_are_welded():
    march = single_ball()
    result = march.polygonize(*BOUNDS)
    vertices = result["vertices"]
    assert len(vertices) > 0
    assert len(vertices) == len(set(vertices))


def test_indices_are_valid():
    march = single_ball()
    result = march.polygonize(*BOUNDS)
    vertices = result["vertices"]
    indices = result["indices"]
    assert len(indices) % 3 == 0
    assert all(isinstance(index, int) for index in indices)
    assert all(0 <= index < len(vertices) for index in indices)


def test_polygonize_is_deterministic():
    march = single_ball()
    first = march.polygonize(*BOUNDS)
    second = march.polygonize(*BOUNDS)
    assert first["vertices"] == second["vertices"]
    assert first["indices"] == second["indices"]


def test_stats_match_output_arrays():
    march = single_ball()
    result = march.polygonize(*BOUNDS)
    stats = result["stats"]
    assert stats["balls"] == 1
    assert stats["vertices"] == len(result["vertices"])
    assert stats["indices"] == len(result["indices"])


def test_adaptive_depth_step_is_at_most_one():
    march = mm.MetaballMarch(max_depth=3)
    march.add_ball((-0.4, 0.0, 0.0), 0.6)
    march.add_ball((0.4, 0.0, 0.0), 0.6)
    result = march.polygonize(*BOUNDS)
    assert result["stats"]["max_depth_delta"] <= 1


def test_budget_limits_samples_without_truncating():
    march = mm.MetaballMarch(max_depth=4, budget_ms=8.0, sample_cost_ms=0.002)
    for offset in (-0.5, 0.0, 0.5):
        march.add_ball((offset, 0.0, 0.0), 0.8)
    result = march.polygonize(*BOUNDS)
    stats = result["stats"]
    assert stats["estimated_ms"] <= 8.0 + 1e-9
    assert stats["truncated"] is False
    assert len(result["vertices"]) > 0


def test_output_is_finite():
    march = single_ball()
    result = march.polygonize(*BOUNDS)
    for vertex in result["vertices"]:
        assert all(math.isfinite(component) for component in vertex)
    for normal in result["normals"]:
        assert all(math.isfinite(component) for component in normal)


def test_concurrent_polygonize_matches():
    march = single_ball()
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(index):
        barrier.wait()
        results[index] = march.polygonize(*BOUNDS)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0]["vertices"] == results[1]["vertices"]
    assert results[0]["indices"] == results[1]["indices"]
