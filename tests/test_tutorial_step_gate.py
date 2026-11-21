import copy

import pytest

import tutorial_step_gate as tsg


TUTORIALS = {
    "t1": {
        "steps": [
            {"id": "s1", "unlock": "feature_a", "reward": "r1"},
            {"id": "s2", "unlock": "feature_b", "reward": "r2"},
            {"id": "s3", "unlock": "feature_c", "reward": "r3"},
        ],
        "skip_level": 10,
        "skip_quest": "quest_intro",
    },
    "t2": {
        "steps": [
            {"id": "x1", "unlock": "feature_x", "reward": "rx1"},
            {"id": "x2", "unlock": "feature_y", "reward": "rx2"},
        ],
        "skip_level": 10,
        "skip_quest": "quest_intro",
    },
    "acc": {
        "steps": [
            {"id": "a1", "unlock": "feature_acc_a", "reward": "ra1"},
            {"id": "a2", "unlock": "feature_acc_b", "reward": "ra2"},
        ],
        "skip_level": 1,
        "skip_quest": "",
    },
}


def gate(version=1, state=None, tutorials=None):
    return tsg.TutorialGate(tutorials=tutorials or TUTORIALS,
                            version=version, state=state)


def start(g, tutorial="t1", scope="character"):
    return g.start("c1", tutorial, scope=scope)


def test_start_returns_first_step():
    g = gate()
    result = start(g)
    assert result["step"] == "s1"
    assert result["completed"] == []
    assert result["done"] is False


def test_advance_moves_to_next_step_in_order():
    g = gate()
    start(g)
    result = g.advance("c1", "t1", "s1")
    assert result["reward"] == "r1"
    assert result["completed"] == ["s1"]
    assert result["step"] == "s2"
    assert result["duplicate"] is False


def test_out_of_order_advance_is_rejected():
    g = gate()
    start(g)
    with pytest.raises(ValueError):
        g.advance("c1", "t1", "s2")


def test_resume_returns_persisted_step_after_reconnect():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    result = g.resume("c1", "t1")
    assert result["step"] == "s2"
    assert result["completed"] == ["s1"]


def test_repeated_advance_grants_reward_once():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    again = g.advance("c1", "t1", "s1")
    assert again["duplicate"] is True
    assert again["reward"] is None
    assert again["step"] == "s2"
    assert again["rewards"] == ["r1"]


def test_skip_unlocks_all_following_features():
    g = gate()
    start(g)
    result = g.skip("c1", "t1", level=10, quests=["quest_intro"])
    assert result["skipped"] is True
    assert sorted(result["unlocked"]) == ["feature_a", "feature_b", "feature_c"]
    assert result["completed"] == ["s1", "s2", "s3"]
    assert g.resume("c1", "t1")["done"] is True


def test_skip_rejects_low_level():
    g = gate()
    start(g)
    with pytest.raises(ValueError):
        g.skip("c1", "t1", level=5, quests=["quest_intro"])


def test_skip_rejects_missing_prerequisite_quest():
    g = gate()
    start(g)
    with pytest.raises(ValueError):
        g.skip("c1", "t1", level=10, quests=[])


def test_second_tutorial_is_rejected_while_one_active():
    g = gate()
    start(g, "t1")
    with pytest.raises(ValueError):
        g.start("c1", "t2")


def test_account_and_character_scopes_are_independent():
    g = gate()
    g.start("c1", "t1", scope="character")
    g.start("u1", "acc", scope="account")
    g.advance("c1", "t1", "s1")
    g.advance("c1", "t1", "s2")
    g.advance("c1", "t1", "s3")
    account = g.resume("u1", "acc", scope="account")
    character = g.resume("c1", "t1", scope="character")
    assert account["step"] == "a1"
    assert character["done"] is True


def test_snapshot_keeps_mid_tutorial_step():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    snapshot = g.snapshot()
    restored = gate(state=snapshot)
    assert restored.resume("c1", "t1")["step"] == "s2"


def test_completion_marker_survives_snapshot():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    g.advance("c1", "t1", "s2")
    g.advance("c1", "t1", "s3")
    snapshot = g.snapshot()
    restored = gate(state=snapshot)
    resumed = restored.resume("c1", "t1")
    assert resumed["done"] is True
    assert restored.start("c1", "t1")["done"] is True


def test_version_upgrade_adds_step_without_resetting_progress():
    g = gate(version=1)
    start(g)
    g.advance("c1", "t1", "s1")
    g.advance("c1", "t1", "s2")
    g.advance("c1", "t1", "s3")
    snapshot = g.snapshot()
    upgraded = copy.deepcopy(TUTORIALS)
    upgraded["t1"]["steps"].append(
        {"id": "s4", "unlock": "feature_d", "reward": "r4"}
    )
    restored = gate(version=2, state=snapshot, tutorials=upgraded)
    result = restored.resume("c1", "t1")
    assert result["step"] == "s4"
    assert result["done"] is False
    assert result["completed"] == ["s1", "s2", "s3"]


def test_transfer_state_moves_progress_to_new_character():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    g.advance("c1", "t1", "s2")
    g.transfer_state("c1", "c2")
    assert g.resume("c2", "t1")["step"] == "s3"
    with pytest.raises(ValueError):
        g.resume("c1", "t1")


def test_finishing_last_step_marks_done():
    g = gate()
    start(g)
    g.advance("c1", "t1", "s1")
    g.advance("c1", "t1", "s2")
    final = g.advance("c1", "t1", "s3")
    assert final["done"] is True
    assert final["step"] is None
    assert final["completed"] == ["s1", "s2", "s3"]
    assert g.resume("c1", "t1")["done"] is True
