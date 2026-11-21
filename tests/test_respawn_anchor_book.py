import pytest

import respawn_anchor_book as rab


WALKABLE = {(0, 0), (10, 0), (20, 0)}
ANCHORS = {"a1": (10, 0), "a2": (20, 0), "bad": (99, 99)}
BOSSES = {"boss1": ["trash1", "trash2"]}


def build(instance_id="i1", team_id="t1", difficulty="normal"):
    book = rab.RespawnAnchor()
    book.create_instance(
        instance_id, "d1", team_id, difficulty=difficulty,
        entrance=(0, 0), walkable=WALKABLE, anchors=ANCHORS, bosses=BOSSES,
    )
    return book


def test_wipe_without_anchor_respawns_everyone_at_the_entrance():
    book = build()
    assert book.current_anchor("i1")["anchor_id"] == "entrance"
    assert book.current_anchor("i1")["position"] == (0, 0)
    result = book.wipe("i1", now=1.0)
    assert result["wiped"] is True
    assert result["position"] == (0, 0)
    assert book.respawn("i1", "p1")["position"] == (0, 0)


def test_activated_anchor_is_used_by_respawn():
    book = build()
    book.activate("i1", "a1")
    assert book.current_anchor("i1")["anchor_id"] == "a1"
    result = book.respawn("i1", "p1")
    assert result["anchor_id"] == "a1"
    assert result["position"] == (10, 0)


def test_leader_change_does_not_move_the_anchor():
    book = build()
    book.activate("i1", "a2")
    book.bind("i1", "p1")
    book.change_leader("i1", "p2")
    assert book.current_anchor("i1")["anchor_id"] == "a2"
    restored = book.restore("i1", "p1")
    assert restored["anchor_id"] == "a2"
    assert book.respawn("i1", "p1")["position"] == (20, 0)


def test_boss_reset_also_resets_linked_trash():
    book = build()
    book.activate("i1", "a1")
    book.kill_boss("i1", "boss1", ["p1"])
    assert book.state("i1", "boss1") == "dead"
    assert book.state("i1", "trash1") == "dead"
    assert book.state("i1", "trash2") == "dead"
    book.reset_boss("i1", "boss1")
    assert book.state("i1", "boss1") == "alive"
    assert book.state("i1", "trash1") == "alive"
    assert book.state("i1", "trash2") == "alive"
    assert book.current_anchor("i1")["anchor_id"] == "a1"


def test_activation_rejects_unwalkable_or_unknown_anchor():
    book = build()
    book.activate("i1", "a1")
    with pytest.raises(ValueError):
        book.activate("i1", "bad")
    assert book.current_anchor("i1")["anchor_id"] == "a1"
    with pytest.raises(ValueError):
        book.activate("i1", "missing")


def test_boss_kill_and_anchor_activation_are_atomic():
    book = build()
    with pytest.raises(ValueError):
        book.kill_boss("i1", "boss1", ["p1"], anchor_id="bad")
    assert book.state("i1", "boss1") == "alive"
    assert book.current_anchor("i1")["anchor_id"] == "entrance"
    assert book.loot_eligible("i1", "boss1", "p1") is False
    book.kill_boss("i1", "boss1", ["p1"], anchor_id="a2")
    assert book.state("i1", "boss1") == "dead"
    assert book.current_anchor("i1")["anchor_id"] == "a2"


def test_normal_and_heroic_difficulty_progress_is_isolated():
    book = rab.RespawnAnchor()
    for instance_id, difficulty in (("n1", "normal"), ("h1", "heroic")):
        book.create_instance(
            instance_id, "d1", "t1", difficulty=difficulty,
            entrance=(0, 0), walkable=WALKABLE, anchors=ANCHORS, bosses=BOSSES,
        )
    book.activate("n1", "a2")
    assert book.current_anchor("h1")["anchor_id"] == "entrance"
    book.kill_boss("n1", "boss1", ["p1"])
    assert book.state("h1", "boss1") == "alive"


def test_reconnect_restores_the_persisted_anchor():
    book = build()
    book.activate("i1", "a2")
    book.bind("i1", "p1")
    book.change_leader("i1", "p2")
    restored = book.restore("i1", "p1")
    assert restored["anchor_id"] == "a2"
    assert restored["position"] == (20, 0)


def test_late_joiner_is_bound_to_the_current_anchor():
    book = build()
    book.activate("i1", "a2")
    joined = book.join_late("i1", "p3")
    assert joined["anchor_id"] == "a2"
    assert book.respawn("i1", "p3")["position"] == (20, 0)


def test_wipe_detection_is_independent_of_the_timer():
    book = build()
    timer = book.start_wipe_timer("i1", 60, now=0.0)
    assert timer["ends_at"] == 60.0
    result = book.wipe("i1", now=10.0)
    assert result["wiped"] is True
    assert result["wipe_count"] == 1
    assert book.respawn("i1", "p1", now=20.0)["position"] == (0, 0)


def test_loot_eligibility_uses_the_roster_snapshot_at_kill():
    book = build()
    book.kill_boss("i1", "boss1", ["p1", "p2"])
    assert book.loot_eligible("i1", "boss1", "p1") is True
    assert book.loot_eligible("i1", "boss1", "p2") is True
    book.join_late("i1", "p3")
    assert book.loot_eligible("i1", "boss1", "p3") is False
    book.reset_boss("i1", "boss1")
    assert book.loot_eligible("i1", "boss1", "p1") is False


def test_instance_reset_clears_anchor_and_boss_progress():
    book = build()
    book.activate("i1", "a2")
    book.bind("i1", "p1")
    book.kill_boss("i1", "boss1", ["p1"])
    book.reset_instance("i1")
    assert book.current_anchor("i1")["anchor_id"] == "entrance"
    assert book.state("i1", "boss1") == "alive"
    assert book.restore("i1", "p1")["anchor_id"] == "entrance"


def test_bind_is_per_player_and_older_anchor_is_kept():
    book = build()
    book.activate("i1", "a1")
    book.bind("i1", "p1")
    book.activate("i1", "a2")
    assert book.restore("i1", "p1")["anchor_id"] == "a1"
    assert book.restore("i1", "p2")["anchor_id"] == "a2"
    assert book.respawn("i1", "p1")["position"] == (10, 0)
    assert book.respawn("i1", "p2")["position"] == (20, 0)


def test_different_teams_in_the_same_dungeon_are_isolated():
    book = rab.RespawnAnchor()
    for instance_id, team_id in (("i1", "t1"), ("i2", "t2")):
        book.create_instance(
            instance_id, "d1", team_id, difficulty="normal",
            entrance=(0, 0), walkable=WALKABLE, anchors=ANCHORS, bosses=BOSSES,
        )
    book.activate("i1", "a2")
    assert book.current_anchor("i2")["anchor_id"] == "entrance"
    book.kill_boss("i1", "boss1", ["p1"])
    assert book.state("i2", "boss1") == "alive"


def test_failed_activation_does_not_change_bound_progress():
    book = build()
    book.activate("i1", "a1")
    book.bind("i1", "p1")
    with pytest.raises(ValueError):
        book.activate("i1", "bad")
    assert book.restore("i1", "p1")["anchor_id"] == "a1"
    assert book.respawn("i1", "p1")["position"] == (10, 0)
