import pytest

import world_line_handoff as wh


def world(capacity=100):
    return wh.WorldLineHandoff(capacity=capacity)


def test_transfer_carries_pets_and_summons():
    game = world()
    game.spawn(1, "p1", kind="player")
    game.spawn(1, "pet1", kind="pet", owner="p1")
    game.spawn(1, "summon1", kind="summon", owner="p1")
    game.transfer(1, "p1", 2)
    assert set(game.entities(2)) == {"p1", "pet1", "summon1"}
    assert game.entities(1) == []


def test_same_account_cannot_exist_on_two_lines():
    game = world()
    game.spawn(1, "p1")
    game.transfer(1, "p1", 2)
    with pytest.raises(ValueError):
        game.attack(1, "p1")
    assert game.attack(2, "p1") is True


def test_cooldown_is_absolute_across_lines():
    game = world()
    game.spawn(1, "p1", cooldown_until=12.5)
    game.transfer(1, "p1", 2, now=3.0)
    assert game.recover("p1")["cooldown_until"] == pytest.approx(12.5)


def test_items_are_not_duplicated_by_transfer():
    game = world()
    game.spawn(1, "p1", items=("sword", "gem"))
    game.transfer(1, "p1", 2)
    lines = game.item_lines()
    assert sorted(lines) == ["gem", "sword"]
    assert lines["sword"] == 2
    assert lines["gem"] == 2


def test_population_updates_immediately():
    game = world()
    game.spawn(1, "p1")
    game.spawn(1, "pet1", kind="pet", owner="p1")
    assert game.population(1) == 2
    assert game.aoi(1, (0.0, 0.0), 1.0) == ["p1", "pet1"]
    game.transfer(1, "p1", 2)
    assert game.population(1) == 0
    assert game.population(2) == 2
    assert game.aoi(1, (0.0, 0.0), 1.0) == []


def test_full_target_line_rejects_transfer():
    game = world(capacity=1)
    game.spawn(1, "p1")
    game.spawn(2, "resident")
    with pytest.raises(ValueError):
        game.transfer(1, "p1", 2)
    assert "p1" in game.entities(1)
    assert "p1" not in game.entities(2)


def test_boss_aggro_moves_with_entity():
    game = world()
    game.spawn(1, "boss", kind="boss")
    game.spawn(1, "p1")
    game.add_aggro(1, "boss", "p1")
    game.transfer(1, "boss", 2)
    assert game.aggro_targets(2, "boss") == ["p1"]
    assert game.aggro_targets(1, "boss") == []


def test_mail_follows_character():
    game = world()
    game.spawn(1, "p1", mail=("letter-1", "letter-2"))
    before = game.mailbox("p1")
    game.transfer(1, "p1", 2)
    assert game.mailbox("p1") == before
    assert before["attachments"] == 2


def test_failed_transfer_rolls_back_to_source():
    game = world()
    game.spawn(1, "p1", cooldown_until=5.0, items=("sword",))
    token = game.transfer_out(1, "p1", 2)
    assert "p1" not in game.entities(2)
    game.fail_transfer(token["token"])
    assert "p1" in game.entities(1)
    assert game.recover("p1")["line"] == 1
    assert game.item_lines()["sword"] == 1


def test_drop_ownership_uses_damage_snapshot_after_merge():
    game = world()
    game.spawn(1, "boss", kind="boss")
    game.spawn(1, "p1")
    game.spawn(2, "p2")
    game.record_damage(1, "boss", "p1", 100)
    game.merge(1, 2)
    game.record_damage(2, "boss", "p2", 500)
    assert game.award("boss") == "p1"


def test_guild_channel_resubscribed_after_transfer():
    game = world()
    game.spawn(1, "p1", guild=True)
    game.transfer(1, "p1", 2)
    channels = game.channels("p1")
    assert channels["line"] == 2
    assert "guild" in channels["subscriptions"]


def test_logout_blocks_pending_transfer():
    game = world()
    game.spawn(1, "p1")
    token = game.transfer_out(1, "p1", 2)
    with pytest.raises(ValueError):
        game.logout("p1")
    game.fail_transfer(token["token"])
    assert game.logout("p1") is True


def test_quest_tracker_points_to_new_line():
    game = world()
    game.spawn(1, "p1", quest_line=1)
    game.transfer(1, "p1", 2)
    assert game.quest_tracker("p1")["line"] == 2


def test_duplicate_spawn_is_rejected():
    game = world()
    game.spawn(1, "p1")
    with pytest.raises(ValueError):
        game.spawn(2, "p1")
    assert "p1" not in game.entities(2)


def test_capacity_counts_pets_and_summons():
    game = world(capacity=2)
    game.spawn(1, "p1")
    game.spawn(1, "pet1", kind="pet", owner="p1")
    game.spawn(2, "resident")
    with pytest.raises(ValueError):
        game.transfer(1, "p1", 2)
    assert "pet1" in game.entities(1)
