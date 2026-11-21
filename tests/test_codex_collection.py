import pytest

import codex_collection as cc


ENTRIES = {
    "e1": {"set": "s1", "hidden": False, "points": 10, "season": None},
    "e2": {"set": "s1", "hidden": False, "points": 10, "season": None},
    "e3": {"set": "s1", "hidden": False, "points": 10, "season": None},
    "h1": {"set": None, "hidden": True, "points": 50, "season": None},
    "season_s1": {"set": None, "hidden": False, "points": 5, "season": "S1"},
    "season_s2": {"set": None, "hidden": False, "points": 5, "season": "S2"},
}

SETS = {"s1": {"required": ["e1", "e2", "e3"], "reward": "set_reward"}}


def codex(state=None):
    return cc.CodexCollection(entries=ENTRIES, sets=SETS, season="S1", state=state)


def detail_ids(account):
    return [entry["id"] for entry in account["entries"]]


def test_duplicate_unlock_adds_points_once():
    c = codex()
    first = c.unlock("a", "e1")
    second = c.unlock("a", "e1")
    assert first["added"] is True
    assert second["added"] is False
    assert second["points"] == 10


def test_character_layers_do_not_double_account_points():
    c = codex()
    c.unlock("a", "e1", character_id="c1")
    second = c.unlock("a", "e1", character_id="c2")
    info = c.detail("a")
    assert second["added"] is False
    assert info["points"] == 10
    assert info["characters"] == {"c1": 1, "c2": 1}


def test_merge_takes_union_not_overwrite():
    c = codex()
    c.unlock("a", "e1")
    result = c.merge("a", {"entries": ["e1", "e2"], "claimed_sets": []})
    assert sorted(result["added"]) == ["e2"]
    assert sorted(detail_ids(c.detail("a"))) == ["e1", "e2"]
    assert result["points"] == 20


def test_merge_does_not_double_count_shared_entries():
    c = codex()
    c.unlock("a", "e1")
    c.unlock("a", "e2")
    result = c.merge("a", {"entries": ["e1", "e2", "e3"], "claimed_sets": []})
    assert result["added"] == ["e3"]
    assert result["points"] == 30


def test_hidden_entry_is_not_listed_before_unlock():
    c = codex()
    assert "h1" not in detail_ids(c.detail("a"))


def test_hidden_entry_appears_after_achievement():
    c = codex()
    c.unlock("a", "h1", achieved=True)
    info = c.detail("a")
    assert detail_ids(info) == ["h1"]
    assert info["points"] == 50


def test_backfill_skips_already_claimed_set_reward():
    c = codex()
    for entry in ("e1", "e2", "e3"):
        c.unlock("a", entry)
    c.grant("a", "s1")
    result = c.backfill(["a"])
    assert result["granted"] == []
    assert result["skipped"][0]["set"] == "s1"


def test_backfill_grants_unclaimed_completed_set():
    c = codex()
    for entry in ("e1", "e2", "e3"):
        c.unlock("a", entry)
    result = c.backfill(["a"])
    assert len(result["granted"]) == 1
    assert result["granted"][0]["set"] == "s1"
    assert c.detail("a")["claimed_sets"] == ["s1"]


def test_grant_requires_complete_set():
    c = codex()
    c.unlock("a", "e1")
    with pytest.raises(ValueError):
        c.grant("a", "s1")


def test_grant_is_idempotent():
    c = codex()
    for entry in ("e1", "e2", "e3"):
        c.unlock("a", entry)
    first = c.grant("a", "s1")
    second = c.grant("a", "s1")
    assert first["granted"] is True
    assert second["granted"] is False
    assert second["duplicate"] is True


def test_revoke_keeps_permanent_progress():
    c = codex()
    c.unlock("a", "e1")
    result = c.revoke("a", "e1")
    assert result["retained"] is True
    info = c.detail("a")
    assert detail_ids(info) == ["e1"]
    assert info["points"] == 10


def test_season_locked_entry_is_rejected():
    c = codex()
    with pytest.raises(ValueError):
        c.unlock("a", "season_s2")
    assert c.unlock("a", "season_s1")["added"] is True


def test_detail_cache_version_changes_on_unlock():
    c = codex()
    first = c.detail("a")
    c.unlock("a", "e1")
    second = c.detail("a")
    assert second["version"] > first["version"]
    assert "e1" in detail_ids(second)


def test_leaderboard_and_detail_share_one_data_source():
    c = codex()
    c.unlock("a", "e1")
    c.unlock("a", "e2")
    c.unlock("b", "e3")
    board = c.leaderboard()
    assert [row["account"] for row in board] == ["a", "b"]
    assert board[0]["points"] == c.detail("a")["points"] == 20
    assert board[1]["points"] == c.detail("b")["points"] == 10


def test_backfill_processes_in_batches():
    c = codex()
    result = c.backfill(["a1", "a2", "a3", "a4", "a5"], batch_size=2)
    assert result["processed"] == 5
    assert result["batches"] == 3
