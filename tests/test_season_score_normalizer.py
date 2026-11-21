import pytest

import season_score_normalizer as ssn


def test_modes_are_normalized_onto_one_scale():
    normalizer = ssn.SeasonScoreNormalizer()
    assert normalizer.normalize("arena", 100) == pytest.approx(100.0)
    assert normalizer.normalize("battlefield", 1000) == pytest.approx(10.0)
    assert normalizer.normalize("race", 100) == pytest.approx(10.0)


def test_per_mode_cap_is_applied_before_summing():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "g1", "arena", 100000, started_at=10.0)
    normalizer.add_match("m2", "g1", "battlefield", 1000, started_at=11.0)
    aggregate = normalizer.aggregate()["g1"]
    assert aggregate["modes"]["arena"] == pytest.approx(500.0)
    assert aggregate["modes"]["battlefield"] == pytest.approx(3.0)
    assert aggregate["total"] == pytest.approx(503.0)


def test_total_cap_is_enforced_after_weighting():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1, total_cap=100.0)
    normalizer.add_match("m1", "g1", "arena", 1000, started_at=1.0)
    normalizer.add_match("m2", "g1", "battlefield", 5000, started_at=2.0)
    normalizer.add_match("m3", "g1", "race", 500, started_at=3.0)
    assert normalizer.aggregate()["g1"]["total"] == pytest.approx(100.0)


def test_mode_weights_are_applied_to_normalized_scores():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "g1", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "g1", "battlefield", 100, started_at=2.0)
    normalizer.add_match("m3", "g1", "race", 100, started_at=3.0)
    aggregate = normalizer.aggregate()["g1"]
    assert aggregate["modes"]["arena"] == pytest.approx(50.0)
    assert aggregate["modes"]["battlefield"] == pytest.approx(0.3)
    assert aggregate["modes"]["race"] == pytest.approx(2.0)
    assert aggregate["total"] == pytest.approx(52.3)


def test_participation_threshold_is_judged_separately():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=2)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "a", "arena", 100, started_at=2.0)
    normalizer.add_match("m3", "b", "arena", 100, started_at=3.0)
    aggregate = normalizer.aggregate()
    assert aggregate["a"]["matches"] == 2
    assert aggregate["b"]["matches"] == 1
    assert [entry["player_id"] for entry in normalizer.rank()] == ["a"]
    assert normalizer.cutoff(1.0) == pytest.approx(100.0)


def test_duplicate_match_is_counted_only_once():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    duplicate = normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    assert duplicate["duplicate"] is True
    aggregate = normalizer.aggregate()["a"]
    assert aggregate["matches"] == 1
    assert aggregate["total"] == pytest.approx(50.0)


def test_match_belongs_to_the_season_of_its_start_time():
    normalizer = ssn.SeasonScoreNormalizer(
        min_matches=1, season=2, season_start=1000.0, season_end=2000.0
    )
    old = normalizer.add_match(
        "old", "a", "arena", 100, started_at=900.0,
        ended_at=1100.0, season=1,
    )
    assert old["counted"] is False
    assert old["reason"] == "previous_season"
    normalizer.add_match(
        "new", "a", "arena", 100, started_at=1000.0,
        ended_at=1900.0, season=2,
    )
    assert normalizer.aggregate()["a"]["matches"] == 1


def test_decay_applies_to_every_mode():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "a", "battlefield", 100, started_at=2.0)
    normalizer.add_match("m3", "a", "race", 100, started_at=3.0)
    before = dict(normalizer.aggregate()["a"]["modes"])
    normalizer.apply_decay(0.5)
    after = normalizer.aggregate()["a"]["modes"]
    for mode in ("arena", "battlefield", "race"):
        assert after[mode] == pytest.approx(before[mode] * 0.5)


def test_cross_server_scores_are_aggregated_by_global_character():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "g1", "arena", 100, started_at=1.0, server="s1")
    normalizer.add_match("m2", "g1", "arena", 100, started_at=2.0, server="s2")
    aggregate = normalizer.aggregate()["g1"]
    assert aggregate["matches"] == 2
    assert aggregate["total"] == pytest.approx(100.0)


def test_percentile_cutoff_uses_explicit_rounding():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    for index, (player, score) in enumerate(
        (("a", 100), ("b", 90), ("c", 80), ("d", 70))
    ):
        normalizer.add_match("m" + player, player, "arena", score,
                             started_at=float(index))
    assert [entry["player_id"] for entry in normalizer.rank()] == [
        "a", "b", "c", "d"
    ]
    assert normalizer.cutoff(0.5) == pytest.approx(45.0)
    assert normalizer.cutoff(1.0) == pytest.approx(35.0)


def test_ties_use_a_stable_key_not_display_name():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "b", "arena", 100, started_at=2.0)
    normalizer.set_display_name("a", "Zed")
    normalizer.set_display_name("b", "Alpha")
    assert [entry["player_id"] for entry in normalizer.rank()] == ["a", "b"]
    normalizer.set_display_name("a", "Aaa")
    assert [entry["player_id"] for entry in normalizer.rank()] == ["a", "b"]


def test_incremental_recompute_matches_full_recompute():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "a", "race", 100, started_at=2.0)
    normalizer.add_match("m3", "b", "battlefield", 1000, started_at=3.0)
    incremental = normalizer.recompute(incremental=True)
    full = normalizer.recompute(incremental=False)
    assert incremental["players"] == full["players"]


def test_banned_players_are_excluded_from_cutoff():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "b", "arena", 90, started_at=2.0)
    normalizer.add_match("m3", "cheat", "arena", 10000, started_at=3.0)
    normalizer.set_banned("cheat", True)
    ranked = [entry["player_id"] for entry in normalizer.rank()]
    assert "cheat" not in ranked
    assert normalizer.cutoff(0.5) == pytest.approx(50.0)
    assert normalizer.cutoff(1.0) == pytest.approx(45.0)


def test_reset_archives_the_old_season_snapshot():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1, season=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.reset_season(2, now=10.0)
    snapshot = normalizer.snapshot()
    assert snapshot["season"] == 2
    assert snapshot["aggregate"] == {}
    assert len(snapshot["archive"]) == 1
    archived = snapshot["archive"][0]
    assert archived["season"] == 1
    assert archived["players"]["a"]["modes"]["arena"] == pytest.approx(50.0)


def test_reset_clears_all_three_modes_at_the_same_time():
    normalizer = ssn.SeasonScoreNormalizer(min_matches=1, season=1)
    normalizer.add_match("m1", "a", "arena", 100, started_at=1.0)
    normalizer.add_match("m2", "a", "battlefield", 100, started_at=2.0)
    normalizer.add_match("m3", "a", "race", 100, started_at=3.0)
    normalizer.reset_season(2, now=4.0)
    assert normalizer.aggregate() == {}
    archived = normalizer.snapshot()["archive"][0]["players"]["a"]["modes"]
    assert set(archived) == {"arena", "battlefield", "race"}
