import pytest

import rank_promotion_series as rps


CONFIG = {
    "protection": 100,
    "abandon_cooldown": 50,
    "wins_needed": 2,
    "losses_allowed": 2,
}


def series(pass_count=0, state=None):
    return rps.PromotionSeries(config=CONFIG,
                               inventory={"pass": pass_count},
                               state=state)


def started(s, rank=10, season="S1", now=0):
    return s.start("p1", season, rank, now=now)


def test_duplicate_match_result_counts_once():
    s = series()
    started(s)
    first = s.record("p1", "m1", "win")
    second = s.record("p1", "m1", "win")
    assert first["wins"] == 1
    assert second["duplicate"] is True
    assert second["wins"] == 1


def test_two_wins_finish_the_series():
    s = series()
    started(s)
    first = s.record("p1", "m1", "win")
    second = s.record("p1", "m2", "win")
    assert first["status"] == "active"
    assert second["status"] == "ready"
    assert second["wins"] == 2


def test_failure_resets_series_for_next_attempt():
    s = series()
    started(s)
    s.record("p1", "m1", "loss")
    failed = s.record("p1", "m2", "loss")
    assert failed["status"] == "failed"
    result = s.advance("p1", now=0)
    assert result["status"] == "demoted"
    assert result["rank"] == 9
    fresh = s.start("p1", "S1", 9, now=1)
    assert fresh["wins"] == 0
    assert fresh["losses"] == 0


def test_rank_snapshot_is_frozen_at_start():
    s = series()
    started(s, rank=10)
    s.set_rank("p1", 5)
    result = s.record("p1", "m1", "win")
    assert result["rank"] == 10
    s.record("p1", "m2", "win")
    promoted = s.advance("p1", now=0)
    assert promoted["rank"] == 11


def test_disconnect_loss_still_counts():
    s = series()
    started(s)
    first = s.record("p1", "m1", "loss", disconnected=True)
    assert first["losses"] == 1
    second = s.record("p1", "m2", "loss", disconnected=True)
    assert second["status"] == "failed"


def test_cross_season_settlement_expires_without_promotion():
    s = series()
    started(s, season="S1")
    s.record("p1", "m1", "win")
    result = s.settle("p1", "S2", now=1)
    assert result["status"] == "expired"
    assert result["season"] == "S2"
    fresh = s.start("p1", "S2", 10, now=2)
    assert fresh["wins"] == 0


def test_free_pass_promotes_failed_series():
    s = series(pass_count=1)
    started(s)
    s.record("p1", "m1", "loss")
    s.record("p1", "m2", "loss")
    result = s.advance("p1", now=0, use_pass=True)
    assert result["status"] == "promoted"
    assert result["pass_used"] is True
    assert result["rank"] == 11
    assert result["pass_left"] == 0


def test_free_pass_without_card_changes_nothing():
    s = series(pass_count=0)
    started(s)
    s.record("p1", "m1", "loss")
    s.record("p1", "m2", "loss")
    with pytest.raises(ValueError):
        s.advance("p1", now=0, use_pass=True)
    result = s.advance("p1", now=0)
    assert result["status"] == "demoted"
    assert result["rank"] == 9


def test_advance_promotes_and_sets_protection():
    s = series()
    started(s, rank=10)
    s.record("p1", "m1", "win")
    s.record("p1", "m2", "win")
    result = s.advance("p1", now=5)
    assert result["status"] == "promoted"
    assert result["rank"] == 11
    assert result["protection_until"] == 105


def test_protection_blocks_demotion_within_window():
    s = series()
    started(s, rank=10)
    s.record("p1", "m1", "win")
    s.record("p1", "m2", "win")
    promoted = s.advance("p1", now=0)
    assert promoted["protection_until"] == 100
    s.start("p1", "S1", 11, now=10)
    s.record("p1", "m3", "loss")
    s.record("p1", "m4", "loss")
    result = s.advance("p1", now=20)
    assert result["status"] == "protected"
    assert result["protected"] is True
    assert result["rank"] == 11


def test_protection_expires_at_absolute_time():
    s = series()
    started(s, rank=10)
    s.record("p1", "m1", "win")
    s.record("p1", "m2", "win")
    s.advance("p1", now=0)
    s.start("p1", "S1", 11, now=10)
    s.record("p1", "m3", "loss")
    s.record("p1", "m4", "loss")
    result = s.advance("p1", now=200)
    assert result["status"] == "demoted"
    assert result["protected"] is False
    assert result["rank"] == 10


def test_abort_writes_cooldown():
    s = series()
    started(s)
    result = s.abort("p1", now=0)
    assert result["status"] == "abandoned"
    assert result["cooldown_until"] == 50
    with pytest.raises(ValueError):
        s.start("p1", "S1", 10, now=10)
    fresh = s.start("p1", "S1", 10, now=50)
    assert fresh["wins"] == 0


def test_same_opponent_cannot_appear_three_times():
    s = series()
    started(s)
    s.record("p1", "m1", "win", opponent_id="rival")
    s.record("p1", "m2", "loss", opponent_id="rival")
    with pytest.raises(ValueError):
        s.record("p1", "m3", "win", opponent_id="rival")


def test_snapshot_restores_series_progress():
    s = series()
    started(s)
    s.record("p1", "m1", "win")
    snapshot = s.snapshot()
    restored = series(state=snapshot)
    result = restored.record("p1", "m2", "win")
    assert result["wins"] == 2
    assert result["status"] == "ready"


def test_migrate_moves_progress_to_new_player_id():
    s = series()
    started(s)
    s.record("p1", "m1", "win")
    s.migrate("p1", "p2")
    result = s.record("p2", "m2", "win")
    assert result["wins"] == 2
    assert result["status"] == "ready"
