import pytest

import beat_grid_align as bga


def clicks(period, count=16, amp=10000):
    out = [0] * (period * count)
    for i in range(count):
        out[i * period] = amp
    return out


def clicks_at(start, period, count=16, amp=10000):
    out = [0] * (start + period * (count - 1) + 1)
    for i in range(count):
        out[start + i * period] = amp
    return out


def test_120_bpm_is_detected():
    result = bga.BeatGridAlign().analyze(clicks(22050, 16))
    assert result["bpm"] == pytest.approx(120.0, abs=1.0)


def test_60_bpm_is_not_doubled_to_120():
    result = bga.BeatGridAlign().analyze(clicks(44100, 8))
    assert result["bpm"] == pytest.approx(60.0, abs=1.0)


def test_phase_follows_first_onset_not_zero():
    result = bga.BeatGridAlign().analyze(clicks_at(10000, 22050, 12))
    assert result["phase_samples"] == pytest.approx(10000, abs=2)


def test_intro_silence_does_not_shift_grid_to_zero():
    result = bga.BeatGridAlign().analyze(clicks_at(32 * 22050, 22050, 12))
    assert result["phase_samples"] > 700000


def test_variable_bpm_produces_multiple_segments():
    first = clicks(44100, 8)
    second = clicks(22050, 8, amp=8000)
    result = bga.BeatGridAlign().analyze(first + second)
    assert len(result["segments"]) >= 2
    assert result["segments"][0]["bpm"] == pytest.approx(60.0, abs=2.0)
    assert result["segments"][-1]["bpm"] == pytest.approx(120.0, abs=2.0)


def test_latency_calibration_shifts_grid_and_judgement():
    samples = clicks(22050, 8)
    plain = bga.BeatGridAlign()
    plain.analyze(samples, latency_ms=0.0)
    assert plain.beat_times()[0] == 0
    delayed = bga.BeatGridAlign()
    delayed.analyze(samples, latency_ms=10.0)
    assert delayed.beat_times()[0] == 441


def test_downbeat_prefers_accented_first_beat():
    period = 22050
    samples = [0] * (period * 16)
    for i in range(16):
        samples[i * period] = 20000 if i % 4 == 0 else 9000
    result = bga.BeatGridAlign().analyze(samples)
    assert result["downbeat"] == 0


def test_resampling_recomputes_duration_and_bpm():
    e = bga.BeatGridAlign(sample_rate=48000)
    samples = clicks(22050, 8)
    converted = e.resample(samples, 44100, 48000)
    assert len(converted) == round(len(samples) * 48000 / 44100)
    result = e.analyze(converted)
    assert result["bpm"] == pytest.approx(120.0, abs=1.0)


def test_silence_reports_explicit_result():
    result = bga.BeatGridAlign().analyze([0] * 44100)
    assert result["silent"] is True
    assert result["bpm"] is None


def test_antiphase_stereo_mixdown_is_silent():
    e = bga.BeatGridAlign()
    mixed = e.mixdown([1, 0, -1, 0], [-1, 0, 1, 0])
    assert mixed == [0, 0, 0, 0]
    result = e.analyze(mixed * 10000)
    assert result["silent"] is True


def test_too_short_audio_is_rejected():
    with pytest.raises(ValueError):
        bga.BeatGridAlign().analyze([0] * 100)


def test_quiet_clicks_still_trigger_onsets():
    e = bga.BeatGridAlign()
    onsets = e.detect_onsets(clicks(22050, 8, amp=10))
    assert len(onsets) == 8


def test_beat_grid_uses_integer_samples_without_drift():
    e = bga.BeatGridAlign()
    e.analyze(clicks(22050, 16))
    times = e.beat_times(duration_samples=44100 * 300)
    assert times[-1] == 13230000


def test_snapshot_isolates_reader_from_writer():
    e = bga.BeatGridAlign()
    e.analyze(clicks(22050, 16))
    snap = e.snapshot()
    snap["bpm"] = 999.0
    assert e.snapshot()["bpm"] == pytest.approx(120.0, abs=1.0)


def test_analysis_reports_onsets_and_elapsed():
    result = bga.BeatGridAlign().analyze(clicks(22050, 16))
    assert len(result["onsets"]) == 16
    assert result["elapsed_ms"] >= 0
