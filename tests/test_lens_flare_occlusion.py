import math

import pytest

import lens_flare_occlusion as lfo


CAM = {"position": (0.0, 0.0, 0.0), "forward": (0.0, 0.0, 1.0), "up": (0.0, 1.0, 0.0)}


def flare_of(result):
    assert len(result["flares"]) == 1
    return result["flares"][0]


def test_clear_light_is_visible_with_full_samples():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0, max_samples=8)
    field.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    result = field.update(CAM, dt=0.016)
    flare = flare_of(result)
    assert flare["visible"] is True
    assert flare["visibility"] == pytest.approx(1.0)
    assert flare["samples"] == 8
    assert result["sample_count"] == 8


def test_wall_fully_occludes_light():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    field.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    field.add_occluder((-5.0, -5.0, 5.0), (5.0, 5.0, 6.0))
    flare = flare_of(field.update(CAM, dt=0.016))
    assert flare["visible"] is False
    assert flare["visibility"] == pytest.approx(0.0, abs=1e-9)


def test_thin_railing_blocks_conservatively():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0, max_samples=8)
    field.add_light("sun", (0.0, 0.0, 10.0), radius=2.0)
    field.add_occluder((-0.02, -0.02, 4.9), (0.02, 0.02, 5.1))
    flare = flare_of(field.update(CAM, dt=0.016))
    assert flare["visible"] is False
    assert 0.0 < flare["visibility"] < 1.0


def test_light_behind_camera_is_culled():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    field.add_light("back", (0.0, 0.0, -5.0), radius=1.0)
    assert field.update(CAM, dt=0.016)["flares"] == []


def test_light_on_camera_plane_is_culled():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    field.add_light("edge", (0.0, 0.0, 0.0), radius=1.0)
    assert field.update(CAM, dt=0.016)["flares"] == []


def test_occluded_light_fades_out_smoothly():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0, fade_rate=4.0)
    field.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    assert flare_of(field.update(CAM, dt=0.016))["visibility"] == pytest.approx(1.0)
    field.add_occluder((-5.0, -5.0, 5.0), (5.0, 5.0, 6.0))
    middle = flare_of(field.update(CAM, dt=0.1))["visibility"]
    assert 0.05 < middle < 0.95
    final = flare_of(field.update(CAM, dt=2.0))["visibility"]
    assert final < 0.01


def test_unoccluded_light_fades_back_in():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0, fade_rate=4.0)
    field.add_occluder((-5.0, -5.0, 5.0), (5.0, 5.0, 6.0))
    field.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    assert flare_of(field.update(CAM, dt=0.016))["visibility"] == pytest.approx(0.0)
    field.clear_occluders()
    middle = flare_of(field.update(CAM, dt=0.1))["visibility"]
    assert 0.05 < middle < 0.95
    final = flare_of(field.update(CAM, dt=2.0))["visibility"]
    assert final > 0.99


def test_flare_size_grows_as_fov_narrows():
    wide = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=110.0)
    narrow = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    wide.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    narrow.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    wide_h = flare_of(wide.update(CAM, dt=0.016))["size"][1]
    narrow_h = flare_of(narrow.update(CAM, dt=0.016))["size"][1]
    expected = math.tan(math.radians(35.0)) / math.tan(math.radians(55.0))
    assert wide_h == pytest.approx(narrow_h * expected, rel=0.05)


def test_flare_size_uses_viewport_aspect():
    standard = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    ultrawide = lfo.LensFlareOcclusion(viewport=(2560, 720), fov_deg=70.0)
    standard.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    ultrawide.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    size_a = flare_of(standard.update(CAM, dt=0.016))["size"]
    size_b = flare_of(ultrawide.update(CAM, dt=0.016))["size"]
    assert size_b[1] == pytest.approx(size_a[1], rel=1e-6)
    assert size_b[0] == pytest.approx(2.0 * size_a[0], rel=1e-6)


def test_streak_is_horizontal_and_aspect_corrected():
    standard = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    ultrawide = lfo.LensFlareOcclusion(viewport=(2560, 720), fov_deg=70.0)
    standard.add_light("sun", (1.0, 1.0, 10.0), radius=1.0, streak_stretch=1.0)
    ultrawide.add_light("sun", (1.0, 1.0, 10.0), radius=1.0, streak_stretch=1.0)
    streak_a = flare_of(standard.update(CAM, dt=0.016))["streak"]
    streak_b = flare_of(ultrawide.update(CAM, dt=0.016))["streak"]
    assert streak_a[1] == pytest.approx(0.0, abs=1e-9)
    assert streak_b[1] == pytest.approx(0.0, abs=1e-9)
    assert streak_b[0] == pytest.approx(2.0 * streak_a[0], rel=1e-6)


def test_ghosts_are_sorted_far_to_near():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    field.add_light("near", (0.0, 0.0, 5.0), radius=1.0)
    field.add_light("far", (0.0, 0.0, 20.0), radius=1.0)
    field.add_light("mid", (0.0, 0.0, 10.0), radius=1.0)
    ids = [flare["id"] for flare in field.update(CAM, dt=0.016)["flares"]]
    assert ids == ["far", "mid", "near"]


def test_resize_rebuilds_target_and_moves_coords():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    field.add_light("sun", (2.0, 0.0, 10.0), radius=1.0)
    first = flare_of(field.update(CAM, dt=0.016))["coords"]
    field.resize(2560, 1440)
    second = flare_of(field.update(CAM, dt=0.016))["coords"]
    assert second[0] - 1280.0 == pytest.approx(2.0 * (first[0] - 640.0))
    assert second[1] - 720.0 == pytest.approx(2.0 * (first[1] - 360.0))
    field.resize(2560, 720)
    third = flare_of(field.update(CAM, dt=0.016))["coords"]
    assert third[0] - 1280.0 == pytest.approx(first[0] - 640.0)
    assert third[1] == pytest.approx(first[1])


def test_two_viewports_do_not_share_buffers():
    small = lfo.LensFlareOcclusion(viewport=(640, 360), fov_deg=70.0)
    large = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0)
    small.add_light("sun", (1.0, 0.0, 10.0), radius=1.0)
    large.add_light("sun", (1.0, 0.0, 10.0), radius=1.0)
    small_coords = flare_of(small.update(CAM, dt=0.016))["coords"]
    large_coords = flare_of(large.update(CAM, dt=0.016))["coords"]
    assert large_coords[0] == pytest.approx(2.0 * small_coords[0], rel=1e-6)
    again = flare_of(small.update(CAM, dt=0.016))["coords"]
    assert again == pytest.approx(small_coords)


def test_over_budget_reduces_sample_density():
    field = lfo.LensFlareOcclusion(viewport=(1280, 720), fov_deg=70.0,
                                   max_samples=8, budget_ms=3.0)
    field.add_light("sun", (0.0, 0.0, 10.0), radius=1.0)
    result = field.update(CAM, dt=0.016, elapsed_ms=9.0)
    assert result["degraded"] is True
    assert 1 <= result["sample_count"] < 8
    assert result["notes"]


def test_invalid_viewport_and_fov_are_rejected():
    with pytest.raises(ValueError):
        lfo.LensFlareOcclusion(viewport=(0, 720))
    with pytest.raises(ValueError):
        lfo.LensFlareOcclusion(viewport=(1280, -1))
    with pytest.raises(ValueError):
        lfo.LensFlareOcclusion(fov_deg=0.0)
