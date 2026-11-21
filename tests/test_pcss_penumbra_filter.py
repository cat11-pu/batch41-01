import pytest

import pcss_penumbra_filter as pf


def test_penumbra_formula():
    pcss = pf.PcssPenumbraFilter(light_size=2.0)
    assert pcss.penumbra_size(blocker_depth=1.0, receiver_depth=2.0) == pytest.approx(2.0)


def test_penumbra_scales_with_light_size():
    narrow = pf.PcssPenumbraFilter(light_size=1.0)
    wide = pf.PcssPenumbraFilter(light_size=4.0)
    assert wide.penumbra_size(1.0, 3.0) == pytest.approx(4.0 * narrow.penumbra_size(1.0, 3.0))


def test_penumbra_world_size_ignores_resolution():
    pcss = pf.PcssPenumbraFilter()
    pcss.set_resolution(2048)
    high = pcss.penumbra_size(1.0, 2.0)
    pcss.set_resolution(1024)
    low = pcss.penumbra_size(1.0, 2.0)
    assert high == pytest.approx(low)


def test_guard_texels_halve_when_resolution_drops():
    pcss = pf.PcssPenumbraFilter()
    pcss.set_resolution(2048)
    fine = pcss.guard_texels(1.0)
    pcss.set_resolution(1024)
    coarse = pcss.guard_texels(1.0)
    assert fine == 2 * coarse


def test_opaque_blocker_hides_receiver():
    pcss = pf.PcssPenumbraFilter(light_size=1.0, bias=0.01)
    result = pcss.estimate(2.0, [{"depth": 0.5, "opacity": 1.0}])
    assert result["visibility"] == pytest.approx(0.0)
    assert result["samples"] == 1
    assert result["penumbra"] == pytest.approx(3.0)


def test_translucent_blocker_weights_opacity():
    pcss = pf.PcssPenumbraFilter(bias=0.01)
    result = pcss.estimate(2.0, [{"depth": 1.0, "opacity": 0.25}])
    assert result["visibility"] == pytest.approx(0.75)


def test_bias_ignores_contact_blocker():
    pcss = pf.PcssPenumbraFilter(bias=0.05)
    result = pcss.estimate(2.0, [{"depth": 1.99, "opacity": 1.0}])
    assert result["visibility"] == pytest.approx(1.0)
    assert result["samples"] == 0


def test_blocker_behind_receiver_is_ignored():
    pcss = pf.PcssPenumbraFilter(bias=0.01)
    result = pcss.estimate(2.0, [{"depth": 3.0, "opacity": 1.0}])
    assert result["visibility"] == pytest.approx(1.0)
    assert result["samples"] == 0


def test_zero_light_size_gives_hard_shadow():
    pcss = pf.PcssPenumbraFilter()
    result = pcss.estimate(2.0, [{"depth": 1.0, "opacity": 1.0}], light_size=0.0)
    assert result["visibility"] == pytest.approx(0.0)
    assert result["penumbra"] == pytest.approx(0.0)


def test_sample_count_is_capped():
    pcss = pf.PcssPenumbraFilter(max_samples=4, bias=0.01)
    blockers = [{"depth": 0.5, "opacity": 0.0}] * 4 + [{"depth": 0.5, "opacity": 1.0}] * 6
    result = pcss.estimate(2.0, blockers)
    assert result["samples"] == 4
    assert result["visibility"] == pytest.approx(1.0)


def test_jitter_is_reproducible():
    pcss = pf.PcssPenumbraFilter()
    assert pcss.jitter(3, 8) == pcss.jitter(3, 8)
    assert pcss.jitter(3, 8) != pcss.jitter(4, 8)


def test_jitter_values_are_in_unit_range():
    pcss = pf.PcssPenumbraFilter()
    values = pcss.jitter(7, 16)
    assert len(values) == 16
    assert all(0.0 <= value < 1.0 for value in values)


def test_no_blockers_gives_full_visibility():
    pcss = pf.PcssPenumbraFilter()
    result = pcss.estimate(2.0, [])
    assert result["visibility"] == pytest.approx(1.0)
    assert result["penumbra"] == pytest.approx(0.0)
    assert result["samples"] == 0


def test_atlas_tiles_are_unique_and_stable():
    pcss = pf.PcssPenumbraFilter(tile_capacity=4)
    first = pcss.tile_for("lamp_a")
    second = pcss.tile_for("lamp_b")
    assert first != second
    assert pcss.tile_for("lamp_a") == first


def test_atlas_tile_overflow_is_rejected():
    pcss = pf.PcssPenumbraFilter(tile_capacity=1)
    pcss.tile_for("lamp_a")
    with pytest.raises(ValueError):
        pcss.tile_for("lamp_b")
