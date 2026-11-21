import pytest

import time_stretch_wsola as tsw


def engine():
    return tsw.TimeStretchWsola(sample_rate=48000, channels=2, window_ms=20)


def test_output_length_is_exact_at_1_5x():
    plan = engine().plan([0.1] * 48000, 3, 2)
    assert plan["output_samples"] == 32000


def test_awkward_ratio_does_not_drift():
    plan = engine().plan([0.0] * 1000000, 1001, 1000)
    assert plan["output_samples"] == 999000


def test_window_and_hop_follow_rate():
    plan = engine().plan([0.1] * 8000, 2, 1)
    assert plan["hop_out"] == 480
    assert plan["hop_in"] == 960
    assert plan["analysis_window"] == 1440


def test_transient_detection_finds_impulses():
    samples = [0.0] * 4000
    samples[1000] = 1.0
    samples[2500] = 0.9
    transients = engine().detect_transients(samples)
    assert 1000 in transients
    assert 2500 in transients


def test_plan_keeps_transients_unstretched():
    samples = [0.0] * 4000
    samples[1000] = 1.0
    plan = engine().plan(samples, 3, 2)
    assert plan["transient_count"] == 1
    assert plan["preserve_transients"] is True


def test_process_returns_planned_sample_count():
    e = engine()
    samples = [0.1] * 4800
    plan = e.plan(samples, 3, 2)
    result = e.process(samples, 3, 2, plan=plan)
    assert result["output_samples"] == 3200
    assert len(result["audio"]) == 3200


def test_seek_resets_phase_and_prerolls():
    result = engine().seek(1234)
    assert result["position"] == 1234
    assert result["phase"] == 0
    assert result["preroll_samples"] > 0


def test_silence_lowers_stitch_strength():
    plan = engine().plan([0.0] * 4800, 3, 2)
    assert plan["silence_ratio"] == pytest.approx(1.0)
    assert plan["overlap_strength"] == pytest.approx(0.5)


def test_channels_share_alignment_offsets():
    e = engine()
    samples = [0.1] * 9600
    result = e.process(samples, 1, 1)
    assert len(result["channel_offsets"]) == 2
    assert len(set(result["channel_offsets"])) == 1


def test_stretch_then_resample_order_is_fixed():
    e = engine()
    plain = e.plan([0.1] * 4800, 3, 2)
    converted = e.plan([0.1] * 4800, 3, 2, resample_to=44100)
    assert plain["stage_order"] == ["stretch"]
    assert converted["stage_order"] == ["stretch", "resample"]
    assert plain["pitch_compensation"] is False
    assert converted["pitch_compensation"] is True


def test_zero_rate_is_rejected():
    with pytest.raises(ValueError):
        engine().plan([0.1] * 4800, 0, 1)


def test_negative_rate_is_rejected():
    with pytest.raises(ValueError):
        engine().plan([0.1] * 4800, -1, 2)


def test_input_shorter_than_window_is_rejected():
    with pytest.raises(ValueError):
        engine().plan([0.1] * 10, 1, 1)


def test_ring_buffer_positions_advance_atomically():
    e = engine()
    e.push(list(range(100)))
    pulled = e.pull(40)
    stats = e.stats()
    assert len(pulled) == 40
    assert pulled[0] == 0
    assert stats["read_pos"] == 40
    assert stats["write_pos"] == 100
    assert stats["buffered"] == 60


def test_process_reports_rate_latency_and_elapsed():
    result = engine().process([0.1] * 4800, 3, 2)
    assert result["rate"] == pytest.approx(1.5)
    assert isinstance(result["latency_samples"], int)
    assert result["elapsed_ms"] >= 0
