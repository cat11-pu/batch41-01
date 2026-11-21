import math

import pytest

import water_surface_fft as wsf


def make_field(**kwargs):
    field = wsf.WaterSurfaceFft(**kwargs)
    field.update(0.7)
    return field


def test_wave_field_is_periodic_at_world_size():
    field = make_field(world_size=64.0)
    for x, y in ((0.0, 0.0), (3.5, 7.25), (16.2, 40.9)):
        assert field.height_at(x + 64.0, y) == pytest.approx(field.height_at(x, y))
        assert field.height_at(x, y + 64.0) == pytest.approx(field.height_at(x, y))


def test_wave_field_does_not_repeat_at_quarter_tile():
    field = make_field(world_size=64.0)
    differences = []
    for x, y in ((0.0, 0.0), (16.0, 0.0), (5.0, 16.0), (10.0, 32.0), (31.0, 7.0)):
        differences.append(abs(field.height_at(x + 16.0, y) - field.height_at(x, y)))
    assert max(differences) > 1e-9


def test_normal_helper_uses_world_spacing():
    field = make_field()
    close = field.normal_from_heights(0.0, 2.0, 1.0, 1.0, 1.0, 1.0)
    far = field.normal_from_heights(0.0, 2.0, 1.0, 1.0, 2.0, 1.0)
    assert close[0] == pytest.approx(-1.0 / math.sqrt(2.0), abs=1e-6)
    assert close[1] == pytest.approx(1.0 / math.sqrt(2.0), abs=1e-6)
    assert abs(close[0]) > abs(far[0])
    assert far[0] == pytest.approx(-0.5 / math.sqrt(1.25), abs=1e-6)


def test_flat_water_normal_points_up():
    field = make_field()
    assert field.normal_from_heights(1.0, 1.0, 1.0, 1.0, 0.5, 0.5) == pytest.approx((0.0, 1.0, 0.0))
    normal = field.normal_at(3.0, 4.0)
    length = math.sqrt(sum(component * component for component in normal))
    assert length == pytest.approx(1.0, rel=1e-6)


def test_foam_grows_with_wind():
    calm = wsf.WaterSurfaceFft(wind=2.0)
    storm = wsf.WaterSurfaceFft(wind=20.0)
    calm.update(1.0)
    storm.update(1.0)
    calm_count = sum(1 for i in range(32) for j in range(32)
                     if calm.foam_at(i * 2.0, j * 2.0) > 0.2)
    storm_count = sum(1 for i in range(32) for j in range(32)
                      if storm.foam_at(i * 2.0, j * 2.0) > 0.2)
    assert storm_count > calm_count


def test_foam_is_higher_on_crests_than_troughs():
    field = make_field(wind=10.0)
    points = [(x * 2.0, y * 2.0) for x in range(32) for y in range(32)]
    crest = max(points, key=lambda point: field.height_at(*point))
    trough = min(points, key=lambda point: field.height_at(*point))
    assert field.foam_at(*crest) > field.foam_at(*trough)


def test_buoyancy_reports_samples_and_fraction():
    field = make_field()
    result = field.buoyancy((0.0, 0.0, 10.0, 10.0), samples=5)
    assert result["samples"] == 5
    assert 0.0 <= result["submerged"] <= 1.0
    assert math.isfinite(result["height"])


def test_buoyancy_uses_the_hull_area_not_only_the_centre():
    field = make_field()
    field.update(0.37)
    narrow = field.buoyancy((0.0, 0.0, 4.0, 4.0), samples=5)["height"]
    wide = field.buoyancy((-8.0, 0.0, 12.0, 4.0), samples=5)["height"]
    assert abs(narrow - wide) > 1e-7


def test_lod_heights_match_at_boundaries():
    field = make_field()
    for x, y in ((0.0, 0.0), (3.5, 4.5), (20.25, 31.75)):
        assert field.height_at_lod(x, y, 0) == pytest.approx(field.height_at(x, y))
        assert field.height_at_lod(x, y, 3) == pytest.approx(field.height_at(x, y))


def test_reflection_uses_a_single_source():
    field = make_field()
    result = field.update(0.8)
    assert result["reflection"]["sources"] == ["planar"]
    field.set_reflection("ssr")
    result = field.update(0.9)
    assert result["reflection"]["sources"] == ["ssr"]
    assert len(result["reflection"]["sources"]) == 1


def test_zero_wind_gives_still_water():
    field = wsf.WaterSurfaceFft(wind=0.0)
    result = field.update(1.0)
    for x, y in ((0.0, 0.0), (3.2, 4.7), (20.0, 50.0), (63.0, 63.0)):
        value = field.height_at(x, y)
        assert math.isfinite(value)
        assert abs(value) < 1e-12
    assert result["height_range"][0] == pytest.approx(0.0)
    assert result["height_range"][1] == pytest.approx(0.0)


def test_camera_movement_does_not_shift_wave_phase():
    field = wsf.WaterSurfaceFft()
    field.update(1.0, camera=(0.0, 0.0))
    before = field.height_at(3.2, 4.7)
    field.update(1.0, camera=(50.0, -20.0))
    after = field.height_at(3.2, 4.7)
    assert after == pytest.approx(before)


def test_old_revision_height_is_stable_after_update():
    field = wsf.WaterSurfaceFft()
    first_revision = field.update(0.3)["revision"]
    old_height = field.height_at(3.2, 4.7, revision=first_revision)
    second_revision = field.update(1.7)["revision"]
    new_height = field.height_at(3.2, 4.7)
    assert second_revision > first_revision
    assert abs(new_height - old_height) > 1e-9
    assert field.height_at(3.2, 4.7, revision=first_revision) == pytest.approx(old_height)


def test_height_range_matches_samples():
    field = wsf.WaterSurfaceFft()
    result = field.update(0.8)
    samples = [field.height_at(x * 4.0, y * 4.0) for x in range(16) for y in range(16)]
    low, high = result["height_range"]
    assert low <= min(samples) + 1e-9
    assert high >= max(samples) - 1e-9
    assert low <= high


def test_over_budget_reduces_layers_and_reports():
    field = wsf.WaterSurfaceFft(layers=3, budget_ms=3.0)
    result = field.update(1.0, elapsed_ms=7.0)
    assert result["degraded"] is True
    assert result["layers_used"] < 3
    assert result["elapsed_ms"] == pytest.approx(7.0)
