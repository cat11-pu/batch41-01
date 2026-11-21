import pytest

import match_state_rollback as msr


TEAM = ("alice", "bob", "carol", "dave", "erin")


def team_players():
    return {
        player: {
            "rank_before": 1500 + index,
            "hidden_before": 1000 + index,
            "inventory": {},
            "currency": 0,
            "quests": {"wins": 0},
            "achievements": [],
            "claimed_rewards": {},
        }
        for index, player in enumerate(TEAM)
    }


def record_default(manager, data=None, match_id="m1", season=2):
    data = data or team_players()
    manager.record_match(
        match_id,
        "loss",
        data,
        season=season,
        quests_after={player: {"wins": 0, "losses": 1} for player in data},
        achievements_after={player: ["fallen"] for player in data},
        rewards={player: {"gold": 100, "gem": 2} for player in data},
    )
    return data


def new_manager(**kwargs):
    manager = msr.MatchStateRollback(season=2, item_values={"gem": 10, "gold": 1})
    record_default(manager, **kwargs)
    return manager


def test_plan_stores_one_rollback_point_for_every_domain():
    manager = new_manager()
    plan = manager.plan("m1")
    assert plan["match_id"] == "m1"
    assert plan["mode"] == "rollback"
    assert set(plan["players"]) == set(TEAM)
    alice = plan["players"]["alice"]
    for key in (
        "rank_before",
        "rank_after",
        "hidden_before",
        "quests_before",
        "achievements_before",
        "rewards",
        "mail_ids",
    ):
        assert key in alice
    assert plan["plan_id"]


def test_plan_is_deduplicated_by_match_id():
    manager = new_manager()
    first = manager.plan("m1")
    second = manager.plan("m1")
    assert first["plan_id"] == second["plan_id"]
    plan_events = [entry for entry in manager.audit() if entry["action"] == "plan"]
    assert len(plan_events) == 1


def test_cross_season_match_can_only_be_compensated():
    manager = msr.MatchStateRollback(season=2, item_values={"gem": 10})
    record_default(manager, match_id="old", season=1)
    plan = manager.plan("old")
    assert plan["mode"] == "compensation"
    assert plan["reason"] == "cross_season"
    with pytest.raises(ValueError):
        manager.apply(plan["plan_id"])
    result = manager.compensate(plan["plan_id"], reason="cross_season")
    assert len(result["mails"]) == len(TEAM)
    assert manager.query("alice")["rank"] == 1480


def test_apply_rolls_back_the_whole_team():
    manager = new_manager()
    result = manager.apply(manager.plan("m1")["plan_id"])
    assert result["applied"] is True
    for index, player in enumerate(TEAM):
        assert manager.query(player)["rank"] == 1500 + index


def test_apply_restores_quests_and_achievements():
    manager = new_manager()
    manager.apply(manager.plan("m1")["plan_id"])
    alice = manager.query("alice")
    assert alice["quests"] == {"wins": 0}
    assert alice["achievements"] == []


def test_rewards_cannot_be_claimed_after_rollback():
    manager = new_manager()
    plan = manager.plan("m1")
    manager.apply(plan["plan_id"])
    with pytest.raises(ValueError):
        manager.claim_reward("m1", "alice", "gold")
    mail = manager.query("alice")["mails"][0]
    assert mail["status"] == "void"


def test_consumed_items_are_converted_and_available_items_removed():
    manager = msr.MatchStateRollback(season=2, item_values={"gem": 10, "gold": 1})
    data = team_players()
    data["alice"]["inventory"] = {"gold": 100, "gem": 0}
    data["alice"]["claimed_rewards"] = {"gold": 100, "gem": 2}
    data["bob"]["inventory"] = {"gem": 2}
    data["bob"]["claimed_rewards"] = {"gem": 2}
    record_default(manager, data=data)
    manager.apply(manager.plan("m1")["plan_id"])
    alice = manager.query("alice")
    assert alice["inventory"].get("gold", 0) == 0
    assert alice["inventory"].get("gem", 0) == 0
    assert alice["currency"] == 20
    bob = manager.query("bob")
    assert bob["inventory"].get("gem", 0) == 0
    assert bob["currency"] == 0


def test_apply_rerun_is_idempotent():
    manager = new_manager()
    plan = manager.plan("m1")
    first = manager.apply(plan["plan_id"])
    second = manager.apply(plan["plan_id"])
    assert first["applied"] is True
    assert second["applied"] is False
    assert manager.query("alice")["rank"] == 1500
    apply_events = [entry for entry in manager.audit() if entry["action"] == "apply"]
    assert len(apply_events) == 1


def test_rollback_and_compensation_are_mutually_exclusive():
    manager = new_manager()
    plan = manager.plan("m1")
    manager.apply(plan["plan_id"])
    with pytest.raises(ValueError):
        manager.compensate(plan["plan_id"])

    other = new_manager()
    other_plan = other.plan("m1")
    other.compensate(other_plan["plan_id"])
    with pytest.raises(ValueError):
        other.apply(other_plan["plan_id"])


def test_compensation_sends_one_mail_per_player_and_is_idempotent():
    manager = new_manager()
    plan = manager.plan("m1")
    first = manager.compensate(plan["plan_id"])
    assert len(first["mails"]) == len(TEAM)
    for player in TEAM:
        mails = [mail for mail in manager.query(player)["mails"]
                 if mail["status"] == "compensation"]
        assert len(mails) == 1
    second = manager.compensate(plan["plan_id"])
    assert second["already"] is True
    for player in TEAM:
        mails = [mail for mail in manager.query(player)["mails"]
                 if mail["status"] == "compensation"]
        assert len(mails) == 1


def test_audit_records_operator_and_plan_identity():
    manager = new_manager()
    plan = manager.plan("m1", operator="gm-7")
    manager.apply(plan["plan_id"], operator="gm-7")
    entries = manager.audit()
    assert [entry["action"] for entry in entries] == ["plan", "apply"]
    for entry in entries:
        assert entry["operator"] == "gm-7"
        assert entry["match_id"] == "m1"
        assert entry["plan_id"] == plan["plan_id"]
        assert "at" in entry


def test_display_query_is_invalidated_by_apply():
    manager = new_manager()
    assert manager.query("alice")["rank"] == 1480
    manager.apply(manager.plan("m1")["plan_id"])
    assert manager.query("alice")["rank"] == 1500


def test_hidden_mmr_is_recomputed_from_the_new_result():
    manager = new_manager()
    manager.apply(manager.plan("m1")["plan_id"])
    assert manager.query("alice")["hidden"] == 1025


def test_login_is_frozen_while_maintenance_is_active():
    manager = new_manager()
    with manager.maintenance("m1"):
        assert manager.login("alice") is False
    assert manager.login("alice") is True


def test_revert_restores_the_post_match_state_and_is_idempotent():
    manager = new_manager()
    plan = manager.plan("m1")
    manager.apply(plan["plan_id"])
    assert manager.query("alice")["rank"] == 1500
    first = manager.revert(plan["plan_id"])
    assert first["reverted"] is True
    assert manager.query("alice")["rank"] == 1480
    assert manager.query("alice")["hidden"] == 980
    second = manager.revert(plan["plan_id"])
    assert second["reverted"] is False
