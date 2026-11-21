import pytest

import multiroom_clock_sync as mcs


def sync():
    return mcs.MultiroomClockSync(master="living", target_buffer_ms=200.0)


def test_offset_uses_lowest_rtt_sample():
    s = sync()
    s.register("a")
    s.add_sample("a", local_ms=1000, master_ms=990, rtt_ms=10)
    s.add_sample("a", local_ms=2000, master_ms=1980, rtt_ms=50)
    assert s.estimate("a")["offset_ms"] == pytest.approx(10.0)


def test_drift_is_estimated_in_ppm():
    s = sync()
    s.register("a")
    s.add_sample("a", local_ms=0, master_ms=0, rtt_ms=2)
    s.add_sample("a", local_ms=100010, master_ms=100000, rtt_ms=2)
    assert s.estimate("a")["drift_ppm"] == pytest.approx(100.0, abs=1.0)


def test_bluetooth_latency_is_calibrated_and_compensated():
    s = sync()
    s.register("bt", latency_ms=150.0, bluetooth=True)
    s.register("living", latency_ms=0.0)
    s.start("bt", 0)
    s.start("living", 0)
    assert s.estimate("bt")["latency_ms"] == pytest.approx(150.0)
    assert s.render_position("bt", 1000) == pytest.approx(850.0)
    assert s.render_position("living", 1000) == pytest.approx(1000.0)


def test_pause_and_resume_use_timestamps():
    s = sync()
    s.register("a")
    s.start("a", 0)
    assert s.position("a", 5000) == pytest.approx(5000.0)
    s.pause("a", 5000)
    assert s.position("a", 8000) == pytest.approx(5000.0)
    s.resume("a", 8000)
    assert s.position("a", 8000) == pytest.approx(5000.0)
    assert s.position("a", 8100) == pytest.approx(5100.0)


def test_join_fills_target_buffer():
    s = sync()
    s.register("new")
    result = s.join("new")
    assert result["state"] == "buffering"
    assert result["buffer_ms"] >= 200.0


def test_resample_ratio_aligns_device_rate():
    s = sync()
    s.register("a", sample_rate=44100)
    assert s.resample_ratio("a") == pytest.approx(48000.0 / 44100.0)


def test_deep_buffer_is_rate_limited_on_catchup():
    s = sync()
    s.register("a")
    deep = s.catchup_rate("a", 500.0)
    normal = s.catchup_rate("a", 200.0)
    assert 1.0 < deep <= 1.05
    assert normal == pytest.approx(1.0)


def test_master_migration_keeps_positions_continuous():
    s = sync()
    s.register("a")
    s.register("b")
    s.start("a", 0)
    before = s.position("a", 1000)
    s.migrate_master("b", 1000)
    after = s.position("a", 1000)
    assert after == pytest.approx(before)
    assert s.master == "b"


def test_clock_jump_is_smoothed_in_small_steps():
    s = sync()
    s.register("a")
    result = s.correct_clock("a", 200.0)
    assert result["applied_ms"] == pytest.approx(5.0)
    assert result["pending_ms"] == pytest.approx(195.0)


def test_snapshot_is_a_deep_copy():
    s = sync()
    s.register("a")
    snap = s.snapshot()
    snap["devices"]["a"]["offset_ms"] = 999.0
    assert s.estimate("a")["offset_ms"] != 999.0


def test_over_budget_round_downshifts_interval():
    s = sync()
    reports = []
    for i in range(50):
        s.register("d%d" % i)
        reports.append({"device": "d%d" % i, "rtt_ms": 1.0, "offset_ms": 0.0})
    result = s.sync_round(reports, budget_ms=0.01)
    assert result["downshifted"] is True
    assert result["interval_ms"] > 100.0


def test_negative_rtt_is_rejected():
    s = sync()
    s.register("a")
    with pytest.raises(ValueError):
        s.add_sample("a", local_ms=0, master_ms=0, rtt_ms=-1.0)


def test_stale_sample_is_ignored():
    s = sync()
    s.register("a")
    s.add_sample("a", local_ms=100, master_ms=100, rtt_ms=2)
    s.add_sample("a", local_ms=50, master_ms=50, rtt_ms=2)
    assert s.estimate("a")["offset_ms"] == pytest.approx(0.0)
    assert s.estimate("a")["samples"] == 1


def test_unknown_device_is_rejected():
    s = sync()
    with pytest.raises(KeyError):
        s.estimate("ghost")


def test_sync_round_processes_fifty_devices():
    s = sync()
    reports = []
    for i in range(50):
        s.register("d%d" % i)
        reports.append({"device": "d%d" % i, "rtt_ms": 1.0, "offset_ms": 0.0})
    result = s.sync_round(reports, budget_ms=1000.0)
    assert result["processed"] == 50
    assert result["elapsed_ms"] >= 0
