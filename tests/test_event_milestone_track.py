import pytest

import event_milestone_track as emt


BASE = {
    "period": "p1",
    "start": 0,
    "end": 100,
    "personal_cap": 100,
    "global_cap": 1000,
    "tiers": [10, 30, 60],
    "version": 1,
}


def make_track(**extra):
    cfg = dict(BASE)
    cfg.update(extra)
    return emt.MilestoneTrack(cfg)


def test_duplicate_task_award_is_idempotent():
    track = make_track()
    first = track.award("p1", "task-a", 10, now=1)
    second = track.award("p1", "task-a", 10, now=2)
    assert first["applied"] == 10
    assert second["duplicate"] is True
    assert second["applied"] == 0
    assert track.progress("p1")["personal"] == 10


def test_jumping_tier_claim_is_rejected():
    track = make_track()
    track.award("p1", "task-a", 40, now=1)
    with pytest.raises(ValueError):
        track.claim("p1", 2)


def test_claim_before_threshold_is_rejected():
    track = make_track()
    track.award("p1", "task-a", 5, now=1)
    with pytest.raises(ValueError):
        track.claim("p1", 1)


def test_award_outside_window_is_rejected():
    track = make_track()
    with pytest.raises(ValueError):
        track.award("p1", "task-a", 10, now=100)
    with pytest.raises(ValueError):
        track.award("p1", "task-b", 10, now=-1)


def test_cumulative_award_is_clamped_to_personal_cap():
    track = make_track(personal_cap=100)
    track.award("p1", "task-a", 80, now=1)
    second = track.award("p1", "task-b", 80, now=2)
    assert second["applied"] == 20
    assert track.progress("p1")["personal"] == 100


def test_global_award_is_clamped_to_global_cap():
    track = make_track(global_cap=100)
    track.award("p1", "task-a", 80, now=1)
    second = track.award("p2", "task-b", 80, now=2)
    assert second["applied"] == 20
    assert track.progress("p1")["global"] == 100


def test_cross_server_points_aggregate_to_global_character():
    track = emt.MilestoneTrack(dict(BASE), server_map={("s2", "guest"): "hero"})
    track.award("hero", "task-a", 30, now=1)
    moved = track.award("guest", "task-b", 20, now=2, server_id="s2")
    assert moved["personal"] == 50
    assert track.progress("hero")["personal"] == 50
    assert track.progress("hero")["global"] == 50


def test_global_progress_is_not_overwritten_by_personal_progress():
    track = make_track()
    track.award("p1", "task-a", 20, now=1)
    track.award("p2", "task-b", 30, now=2)
    assert track.progress("p1")["personal"] == 20
    assert track.progress("p1")["global"] == 50


def test_compensation_uses_historical_snapshot_points():
    track = make_track()
    track.award("p1", "task-a", 55, now=1)
    track.claim("p1", 1, idempotency_key="first")
    result = track.compensate("p1", {"points": 55, "version": 1})
    assert result["granted"] == [2]
    again = track.compensate("p1", {"points": 55, "version": 1})
    assert again["granted"] == []
    assert again["duplicate"] is True


def test_claim_with_same_key_grants_once():
    track = make_track()
    track.award("p1", "task-a", 40, now=1)
    first = track.claim("p1", 1, idempotency_key="key-1")
    second = track.claim("p1", 1, idempotency_key="key-1")
    third = track.claim("p1", 1)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert third["duplicate"] is True
    assert track.progress("p1")["claimed"] == [1]


def test_config_hot_update_does_not_downgrade_achieved_tiers():
    track = make_track()
    track.award("p1", "task-a", 60, now=1)
    for tier in (1, 2, 3):
        track.claim("p1", tier)
    updated = dict(BASE)
    updated["version"] = 2
    updated["tiers"] = [20, 50, 80]
    result = track.apply_config(updated)
    assert result["version"] == 2
    assert track.progress("p1")["claimed"] == [1, 2, 3]
    track.award("p2", "task-b", 25, now=2)
    assert track.progress("p2")["next_tier"] == 1
    track.claim("p2", 1)
    with pytest.raises(ValueError):
        track.claim("p2", 2)


def test_ranking_points_are_the_same_ledger_as_milestones():
    track = make_track()
    track.award("p1", "task-a", 40, now=1)
    data = track.progress("p1")
    assert data["ranking_points"] == data["personal"] == 40


def test_rotate_clears_current_period_and_archives_claims():
    track = make_track()
    track.award("p1", "task-a", 40, now=1)
    track.claim("p1", 1)
    result = track.rotate("p2")
    assert result["archived_period"] == "p1"
    assert result["period"] == "p2"
    data = track.progress("p1")
    assert data["personal"] == 0
    assert data["claimed"] == []
    with pytest.raises(ValueError):
        track.claim("p1", 1)
    again = track.award("p1", "task-a", 40, now=3)
    assert again["applied"] == 40
    assert again["duplicate"] is False


def test_stale_config_version_is_rejected():
    track = make_track()
    newer = dict(BASE)
    newer["version"] = 3
    track.apply_config(newer)
    older = dict(BASE)
    older["version"] = 2
    with pytest.raises(ValueError):
        track.apply_config(older)


def test_progress_reports_next_claimable_tier():
    track = make_track()
    track.award("p1", "task-a", 35, now=1)
    data = track.progress("p1")
    assert data["personal"] == 35
    assert data["claimed"] == []
    assert data["next_tier"] == 1
    track.claim("p1", 1)
    assert track.progress("p1")["next_tier"] == 2
    track.award("p1", "task-b", 30, now=2)
    assert track.progress("p1")["next_tier"] == 2
    track.claim("p1", 2)
    assert track.progress("p1")["next_tier"] == 3
