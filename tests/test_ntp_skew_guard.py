import pytest

import ntp_skew_guard as nsg


def _resp(
    ident,
    stratum=1,
    jitter=0.1,
    t1=0.0,
    t2=0.1,
    t3=0.1,
    t4=0.2,
    reachable=True,
):
    return {
        "id": ident,
        "stratum": stratum,
        "jitter": jitter,
        "t1": t1,
        "t2": t2,
        "t3": t3,
        "t4": t4,
        "reachable": reachable,
    }


def test_certificate_expiry_uses_wall_clock_after_a_jump():
    gate = nsg.ClockSkewGuard(wall=100.0, mono=100.0)
    assert gate.sync([_resp("a")])["ok"] is True
    assert gate.certificate_valid(0, 500) is True
    gate.apply_step(1000.0)
    assert gate.certificate_valid(0, 500) is False


def test_audit_timestamps_never_run_backwards_after_rollback():
    gate = nsg.ClockSkewGuard(wall=1000.0, mono=0.0)
    first = gate.audit_timestamp()
    gate.apply_step(500.0)
    second = gate.audit_timestamp()
    assert second >= first
    assert gate.wall_now() == 500.0


def test_unreachable_sources_leave_time_unchanged_and_raise_an_alert():
    gate = nsg.ClockSkewGuard(wall=100.0, mono=10.0)
    result = gate.sync([_resp("a", reachable=False), _resp("b", reachable=False)])
    assert result["ok"] is False
    assert result["code"] == "no_source"
    assert gate.wall_now() == 100.0
    assert any(entry.get("code") == "no_source" for entry in gate.audit)


def test_leap_second_timestamp_is_clamped():
    gate = nsg.ClockSkewGuard()
    assert gate.normalize_timestamp(59.5) == 59.5
    clamped = gate.normalize_timestamp(60.0, leap=True)
    assert 59.0 <= clamped < 60.0


def test_source_selection_prefers_stratum_over_response_speed():
    gate = nsg.ClockSkewGuard(max_offset=5.0)
    fast = _resp("fast", stratum=2, jitter=0.1, t1=0, t2=0.1, t3=0.1, t4=0.2)
    stable = _resp("stable", stratum=1, jitter=5.0, t1=0, t2=1, t3=1, t4=2)
    result = gate.sync([fast, stable])
    assert result["ok"] is True
    assert result["source"] == "stable"


def test_sensitive_actions_fail_closed_before_the_first_sync():
    gate = nsg.ClockSkewGuard(wall=100.0)
    assert gate.sensitive_ready() is False
    assert gate.certificate_valid(0, 200) is False
    assert gate.sync([_resp("a")])["ok"] is True
    assert gate.sensitive_ready() is True
    assert gate.certificate_valid(0, 200) is True


def test_task_schedule_uses_monotonic_time_not_wall_time():
    gate = nsg.ClockSkewGuard(wall=0.0, mono=0.0)
    gate.advance_mono(10.0)
    assert gate.task_due(0.0, 30.0) is False
    gate.apply_step(3600.0)
    assert gate.task_due(0.0, 30.0) is False
    gate.advance_mono(20.0)
    assert gate.task_due(0.0, 30.0) is True


def test_log_time_is_utc_regardless_of_container_timezone():
    east = nsg.ClockSkewGuard(wall=1700000000.0, tz="+08:00")
    west = nsg.ClockSkewGuard(wall=1700000000.0, tz="-05:00")
    assert east.format_log_time(1700000000.0) == west.format_log_time(1700000000.0)
    assert east.format_log_time(1700000000.0).endswith("Z")


def test_snapshot_is_atomic_and_detached_from_internal_state():
    gate = nsg.ClockSkewGuard(wall=100.0, mono=50.0)
    assert gate.sync([_resp("a")])["ok"] is True
    gate.apply_step(200.0)
    snapshot = gate.snapshot()
    assert snapshot["wall"] == 200.0
    assert snapshot["mono"] == 50.0
    snapshot["wall"] = 999.0
    assert gate.wall_now() == 200.0


def test_offset_beyond_threshold_rejects_requests():
    gate = nsg.ClockSkewGuard(wall=0.0, max_offset=1.0)
    big = _resp("big", t1=0.0, t2=2.0, t3=2.0, t4=0.0)
    result = gate.sync([big])
    assert result["skew_exceeded"] is True
    assert gate.request_allowed() is False
    assert gate.sensitive_ready() is False


def test_empty_response_list_is_a_no_source_error():
    gate = nsg.ClockSkewGuard(wall=7.0)
    result = gate.sync([])
    assert result["ok"] is False
    assert result["code"] == "no_source"
    assert gate.wall_now() == 7.0


def test_negative_round_trip_delay_is_clamped_to_zero():
    gate = nsg.ClockSkewGuard(max_offset=5.0)
    result = gate.sync([_resp("neg", t1=10.0, t2=8.0, t3=9.0, t4=10.1)])
    assert result["ok"] is True
    assert result["delay"] == 0.0


def test_wall_jump_is_detected_and_reported():
    gate = nsg.ClockSkewGuard(wall=100.0)
    result = gate.apply_step(1000.0)
    assert result["jump"] is True
    assert result["delta"] == 900.0
    assert gate.apply_step(1001.0)["jump"] is False


def test_each_sync_is_audited_with_source_offset_and_delay():
    gate = nsg.ClockSkewGuard(wall=0.0)
    gate.sync([_resp("a")])
    entry = gate.audit[-1]
    assert entry["source"] == "a"
    assert entry["offset"] == 0.0
    assert entry["delay"] == 0.2


def test_guards_do_not_share_clock_or_audit_state():
    first = nsg.ClockSkewGuard(wall=0.0)
    second = nsg.ClockSkewGuard(wall=100.0)
    assert first.sync([_resp("a")])["ok"] is True
    assert first.sensitive_ready() is True
    assert second.sensitive_ready() is False
    assert second.audit == []
