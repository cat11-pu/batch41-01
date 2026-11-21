import pytest

import mount_evolution as mev


def mounted(mount_id="m1", species="wolf", owner="alice", **kwargs):
    evolution = mev.MountEvolution()
    evolution.add_mount(mount_id, species, owner, materials={"shard": 10}, **kwargs)
    return evolution


def test_evolve_advances_exactly_one_stage():
    evolution = mounted()
    result = evolution.evolve("m1", roll=0.0)
    assert result["success"] is True
    assert result["stage"] == 1
    assert evolution.state("m1")["stage"] == 1


def test_insufficient_materials_raise_without_changing_state():
    evolution = mev.MountEvolution()
    evolution.add_mount("m1", "wolf", "alice", materials={"shard": 1})
    before = evolution.state("m1")
    with pytest.raises(ValueError):
        evolution.evolve("m1", roll=0.0)
    after = evolution.state("m1")
    assert after["stage"] == before["stage"] == 0
    assert after["stock"] == before["stock"] == {"shard": 1}
    assert after["level"] == before["level"] == 1


def test_extra_materials_cannot_skip_a_stage():
    evolution = mounted()
    evolution.evolve("m1", roll=0.0)
    evolution.evolve("m1", roll=0.0)
    state = evolution.state("m1")
    assert state["stage"] == 2
    assert state["stock"]["shard"] == 6


def test_preview_and_settlement_use_the_same_success_rate():
    evolution = mounted()
    preview = evolution.preview("m1")
    assert preview["next_stage"] == 1
    assert preview["success_rate"] == pytest.approx(0.6)
    assert preview["materials"] == {"shard": 2}
    success = evolution.evolve("m1", roll=0.59)
    assert success["success"] is True
    assert success["success_rate"] == pytest.approx(0.6)

    evolution.add_mount("m2", "wolf", "alice", materials={"shard": 2})
    failure = evolution.evolve("m2", roll=0.6)
    assert failure["success"] is False
    assert failure["success_rate"] == pytest.approx(0.6)
    assert evolution.state("m2")["stage"] == 0


def test_failed_roll_keeps_stage_level_and_materials():
    evolution = mounted()
    before = evolution.state("m1")
    result = evolution.evolve("m1", roll=0.9)
    after = evolution.state("m1")
    assert result["success"] is False
    assert after["stage"] == before["stage"]
    assert after["level"] == before["level"]
    assert after["stock"] == before["stock"]


def test_skill_and_appearance_unlock_together():
    evolution = mounted()
    evolution.evolve("m1", roll=0.0)
    state = evolution.state("m1")
    assert state["skills"] == [mev.MountEvolution.SKILLS[1]]
    assert state["appearance"] == mev.MountEvolution.APPEARANCE[1]


def test_evolution_preserves_mount_level():
    evolution = mounted(level=7)
    evolution.evolve("m1", roll=0.0)
    assert evolution.state("m1")["level"] == 7


def test_binding_type_is_preserved_by_evolution():
    evolution = mev.MountEvolution()
    evolution.add_mount("m1", "wolf", "alice", bind_type="character",
                        materials={"shard": 2})
    evolution.add_mount("m2", "wolf", "bob", bind_type="account",
                        materials={"shard": 2})
    evolution.evolve("m1", roll=0.0)
    evolution.evolve("m2", roll=0.0)
    assert evolution.state("m1")["bind_type"] == "character"
    assert evolution.state("m2")["bind_type"] == "account"


def test_speed_bonus_has_a_hard_cap_with_team_aura():
    evolution = mounted(speed=0.5)
    assert evolution.speed("m1") == pytest.approx(0.5)
    assert evolution.speed("m1", team_aura=0.6) == pytest.approx(1.0)


def test_codex_is_deduplicated_by_species():
    evolution = mev.MountEvolution()
    evolution.add_mount("m1", "wolf", "alice")
    evolution.add_mount("m2", "wolf", "alice")
    entries = evolution.codex("alice")
    assert len(entries) == 1
    assert entries[0]["species"] == "wolf"
    assert entries[0]["count"] == 2


def test_release_refunds_investment_and_clears_codex():
    evolution = mev.MountEvolution()
    evolution.add_mount("m1", "wolf", "alice")
    evolution.add_mount("m2", "wolf", "alice")
    evolution.feed("m1", "growth", value=3)
    evolution.feed("m1", "growth", value=4)
    first = evolution.release("m1")
    assert first["refund"] == 7
    entries = evolution.codex("alice")
    assert len(entries) == 1
    assert entries[0]["count"] == 1
    evolution.release("m2")
    assert evolution.codex("alice") == []


def test_evolution_is_blocked_while_riding():
    evolution = mounted()
    assert evolution.mount_up("m1")["riding"] is True
    with pytest.raises(ValueError):
        evolution.evolve("m1", roll=0.0)
    assert evolution.state("m1")["stage"] == 0
    evolution.dismount("m1")
    assert evolution.evolve("m1", roll=0.0)["success"] is True


def test_evolution_lock_prevents_concurrent_requests():
    evolution = mounted()
    token = evolution.begin_evolution("m1")
    assert token
    with pytest.raises(ValueError):
        evolution.begin_evolution("m1")
    assert evolution.finish_evolution(token)["released"] is True
    second = evolution.begin_evolution("m1")
    assert second
    evolution.finish_evolution(second)


def test_max_stage_is_rejected_without_spending_materials():
    evolution = mounted()
    evolution.evolve("m1", roll=0.0)
    evolution.evolve("m1", roll=0.0)
    evolution.evolve("m1", roll=0.0)
    assert evolution.state("m1")["stage"] == 3
    with pytest.raises(ValueError):
        evolution.evolve("m1", roll=0.0)
    assert evolution.state("m1")["stock"]["shard"] == 4


def test_fed_investment_survives_evolution():
    evolution = mounted()
    evolution.feed("m1", "growth", value=5)
    evolution.evolve("m1", roll=0.0)
    released = evolution.release("m1")
    assert released["refund"] == 5
