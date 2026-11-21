import math
import threading

import pytest

import hrtf_panner as hp


def make_table():
    table = {}
    for azimuth in range(-180, 181, 10):
        for elevation in (0, 40):
            table[(float(azimuth), float(elevation))] = {
                "left": 0.5,
                "right": 0.5,
                "itd": 0.0005,
                "ild": 0.0,
            }
    return table


def entry(left, right, itd, ild):
    return {"left": left, "right": right, "itd": itd, "ild": ild}


def test_lookup_interpolates_azimuth():
    table = make_table()
    table[(0.0, 0.0)] = entry(1.0, 0.5, 0.0002, 3.0)
    table[(10.0, 0.0)] = entry(0.5, 1.0, 0.0006, -3.0)
    panner = hp.HrtfPanner(hrtf=table)
    result = panner.lookup(5.0, 0.0)
    assert result["left"] == pytest.approx(0.75)
    assert result["right"] == pytest.approx(0.75)
    assert result["itd"] == pytest.approx(0.0004)
    assert result["ild"] == pytest.approx(0.0)


def test_elevation_interpolation_changes_itd():
    table = make_table()
    table[(0.0, 0.0)] = entry(0.6, 0.6, 0.0002, 1.0)
    table[(0.0, 40.0)] = entry(0.6, 0.6, 0.0008, 1.0)
    panner = hp.HrtfPanner(hrtf=table)
    middle = panner.lookup(0.0, 20.0)
    low = panner.lookup(0.0, 0.0)
    assert middle["itd"] == pytest.approx(0.0005)
    assert middle["itd"] != pytest.approx(low["itd"])


def test_lookup_wraps_shortest_path_across_front():
    table = {
        (350.0, 0.0): entry(1.0, 0.0, 0.0002, 9.0),
        (10.0, 0.0): entry(0.0, 1.0, 0.0002, -9.0),
        (350.0, 40.0): entry(1.0, 0.0, 0.0002, 9.0),
        (10.0, 40.0): entry(0.0, 1.0, 0.0002, -9.0),
    }
    panner = hp.HrtfPanner(hrtf=table)
    result = panner.lookup(0.0, 0.0)
    assert result["left"] == pytest.approx(0.5)
    assert result["right"] == pytest.approx(0.5)


def test_lookup_rejects_invalid_angles():
    panner = hp.HrtfPanner(hrtf=make_table())
    with pytest.raises(ValueError):
        panner.lookup(370.0, 0.0)
    with pytest.raises(ValueError):
        panner.lookup(0.0, 91.0)
    with pytest.raises(ValueError):
        panner.lookup(float("nan"), 0.0)


def test_itd_and_ild_are_separate_fields():
    table = make_table()
    table[(10.0, 0.0)] = entry(0.8, 0.2, 0.0006, 6.0)
    result = hp.HrtfPanner(hrtf=table).lookup(10.0, 0.0)
    assert result["left"] == pytest.approx(0.8)
    assert result["right"] == pytest.approx(0.2)
    assert result["itd"] == pytest.approx(0.0006)
    assert result["ild"] == pytest.approx(6.0)
    assert result["itd"] != pytest.approx(result["ild"])


def test_listener_yaw_changes_relative_azimuth():
    panner = hp.HrtfPanner(hrtf=make_table())
    panner.add_source("s", (0.0, 0.0, -5.0))
    panner.set_listener((0.0, 0.0, 0.0), yaw_deg=0.0)
    front = panner.compute()["sources"]["s"]["azimuth"]
    panner.set_listener((0.0, 0.0, 0.0), yaw_deg=90.0)
    turned = panner.compute()["sources"]["s"]["azimuth"]
    assert front == pytest.approx(0.0)
    assert abs(turned) == pytest.approx(90.0, abs=1e-6)


def test_listener_pose_is_snapshotted_atomically():
    panner = hp.HrtfPanner(hrtf=make_table())
    panner.add_source("s", (0.0, 0.0, -5.0))

    def flip():
        for index in range(300):
            panner.set_listener((0.0, 0.0, 0.0), yaw_deg=0.0 if index % 2 == 0 else 90.0)

    thread = threading.Thread(target=flip)
    thread.start()
    for _ in range(300):
        result = panner.compute()
        yaw = result["pose_used"]["yaw_deg"]
        assert yaw in (0.0, 90.0)
        azimuth = result["sources"]["s"]["azimuth"]
        if yaw == 0.0:
            assert abs(azimuth) < 1e-6
        else:
            assert abs(abs(azimuth) - 90.0) < 1e-6
    thread.join()


def test_near_field_gain_is_applied_once():
    table = make_table()
    table[(0.0, 0.0)] = entry(0.7, 0.6, 0.0005, 1.0)
    panner = hp.HrtfPanner(hrtf=table)
    panner.add_source("s", (0.0, 0.0, -0.2), near_field=True)
    source = panner.compute()["sources"]["s"]
    assert source["near_field_gain"] > 1.0
    expected = source["distance_gain"] * source["near_field_gain"] * 0.7
    assert source["total_left"] == pytest.approx(expected)
    expected_right = source["distance_gain"] * source["near_field_gain"] * 0.6
    assert source["total_right"] == pytest.approx(expected_right)


def test_hrtf_is_resampled_to_engine_rate():
    panner = hp.HrtfPanner(sample_rate=48000, hrtf_sample_rate=44100,
                           hrtf=make_table())
    panner.add_source("s", (0.0, 0.0, -5.0))
    result = panner.compute()
    assert result["resampled"] is True
    assert result["hrtf_rate_used"] == 48000
    native = hp.HrtfPanner(sample_rate=48000, hrtf_sample_rate=48000,
                           hrtf=make_table())
    native.add_source("s", (0.0, 0.0, -5.0))
    assert native.compute()["resampled"] is False


def test_itd_is_converted_to_engine_samples():
    table = make_table()
    table[(0.0, 0.0)] = entry(0.7, 0.7, 0.0005, 0.0)
    panner = hp.HrtfPanner(sample_rate=48000, hrtf=table)
    panner.add_source("s", (0.0, 0.0, -5.0))
    source = panner.compute()["sources"]["s"]
    assert source["itd_samples"] == pytest.approx(24.0)


def test_mono_output_downmixes_both_ears():
    panner = hp.HrtfPanner(hrtf=make_table(), channels=1)
    panner.add_source("s", (3.0, 0.0, -4.0))
    result = panner.compute()
    source = result["sources"]["s"]
    assert result["channels"] == 1
    assert source["mono"] == pytest.approx((source["total_left"] + source["total_right"]) / 2.0)


def test_over_budget_degrades_by_priority():
    panner = hp.HrtfPanner(hrtf=make_table(), budget_ms=1.0)
    for index in range(32):
        angle = math.radians(index * 10.0)
        panner.add_source(index,
                          (5.0 * math.sin(angle), 0.0, -5.0 * math.cos(angle)),
                          priority=index)
    result = panner.compute(elapsed_ms=20.0)
    assert result["degraded"] is True
    assert len(result["sources"]) < 32
    assert 31 in result["sources"]
    assert result["skipped"]
    assert 31 not in result["skipped"]


def test_each_source_reports_gains_and_elapsed_time():
    panner = hp.HrtfPanner(hrtf=make_table())
    panner.add_source("left", (-2.0, 0.0, -5.0))
    panner.add_source("right", (2.0, 0.0, -5.0))
    result = panner.compute(elapsed_ms=1.25)
    assert result["elapsed_ms"] == pytest.approx(1.25)
    assert set(result["pose_used"]) >= {"position", "yaw_deg", "pitch_deg"}
    for source in result["sources"].values():
        for key in ("azimuth", "elevation", "itd", "ild", "itd_samples",
                    "hrtf_left", "hrtf_right", "distance_gain",
                    "near_field_gain", "gain", "total_left", "total_right",
                    "mono"):
            assert key in source


def test_distance_attenuation_decreases_with_distance():
    panner = hp.HrtfPanner(hrtf=make_table())
    panner.add_source("near", (0.0, 0.0, -1.0))
    panner.add_source("far", (0.0, 0.0, -8.0))
    result = panner.compute()["sources"]
    assert result["near"]["distance_gain"] > 0.0
    assert result["near"]["distance_gain"] > result["far"]["distance_gain"]


def test_non_finite_source_position_is_rejected():
    panner = hp.HrtfPanner(hrtf=make_table())
    with pytest.raises(ValueError):
        panner.add_source("bad", (float("nan"), 0.0, 0.0))
    with pytest.raises(ValueError):
        panner.add_source("bad", (0.0, float("inf"), 0.0))
