import pytest

import gear_forge as gf


CONFIG = {
    "success_rate": 0.4,
    "pity_limit": 3,
    "cost": {"ore": 1},
    "stat_per_level": {"attack": 10},
}


def gate(gears=None, inventory=None, config=None, roll=None, state=None):
    cfg = dict(CONFIG)
    if config:
        cfg.update(config)
    return gf.GearForge(gears=gears, inventory=inventory, config=cfg,
                        roll=roll, state=state)


def base_gear(**over):
    gear = {"level": 5, "max_level": 10, "bound": True, "pity": 0}
    gear.update(over)
    return gear


def test_preview_rate_matches_forge_rate():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.1)
    preview = g.preview("w")
    result = g.forge("w", version=0)
    assert preview["success_rate"] == 0.4
    assert result["rate"] == preview["success_rate"]
    assert result["success"] is True


def test_preview_does_not_consume_materials_or_level():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.99)
    preview1 = g.preview("w")
    preview2 = g.preview("w")
    assert preview1["level"] == preview2["level"] == 5
    state = g.snapshot()
    assert state["inventory"]["ore"] == 10
    assert state["gears"]["w"]["level"] == 5
    result = g.forge("w", version=0)
    assert result["materials_used"] == {"ore": 1}
    assert result["success"] is False
    assert result["level"] == 4


def test_max_level_is_rejected_before_consuming_materials():
    g = gate({"w": base_gear(level=10)}, {"ore": 10}, roll=lambda: 0.1)
    with pytest.raises(ValueError):
        g.forge("w", version=0)
    assert g.snapshot()["inventory"]["ore"] == 10


def test_stale_version_is_rejected_without_double_charge():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.1)
    first = g.forge("w", version=0)
    assert first["success"] is True
    with pytest.raises(ValueError):
        g.forge("w", version=0)
    state = g.snapshot()
    assert state["inventory"]["ore"] == 9
    assert state["gears"]["w"]["level"] == 6


def test_success_raises_level_and_consumes_material():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.1)
    result = g.forge("w", version=0)
    assert result["success"] is True
    assert result["level"] == 6
    assert result["materials_used"] == {"ore": 1}
    assert g.snapshot()["inventory"]["ore"] == 9


def test_failure_lowers_level_by_one():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.9)
    result = g.forge("w", version=0)
    assert result["success"] is False
    assert result["level"] == 4
    assert result["pity"] == 1


def test_failure_at_zero_level_stays_zero():
    g = gate({"w": base_gear(level=0)}, {"ore": 10}, roll=lambda: 0.9)
    result = g.forge("w", version=0)
    assert result["success"] is False
    assert result["level"] == 0
    assert result["pity"] == 1


def test_protection_charm_is_consumed_on_failure():
    g = gate({"w": base_gear()}, {"ore": 10, "charm": 1}, roll=lambda: 0.9)
    g.protect("w")
    result = g.forge("w", version=0)
    assert result["success"] is False
    assert result["charm_used"] is True
    assert result["level"] == 5
    assert g.snapshot()["inventory"]["charm"] == 0


def test_pity_forces_success_after_limit():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.9)
    for version in range(3):
        result = g.forge("w", version=version)
        assert result["success"] is False
    assert result["level"] == 2
    assert result["pity"] == 3
    forced = g.forge("w", version=3)
    assert forced["success"] is True
    assert forced["level"] == 3
    assert forced["pity"] == 0


def test_pity_survives_snapshot_round_trip():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.9)
    g.forge("w", version=0)
    g.forge("w", version=1)
    snapshot = g.snapshot()
    restored = gate(state=snapshot, roll=lambda: 0.9)
    third = restored.forge("w", version=2)
    assert third["success"] is False
    assert third["pity"] == 3
    fourth = restored.forge("w", version=3)
    assert fourth["success"] is True
    assert fourth["level"] == 3


def test_bound_state_is_preserved():
    g = gate({"b1": base_gear(bound=True), "b2": base_gear(bound=False)},
             {"ore": 10}, roll=lambda: 0.1)
    first = g.forge("b1", version=0)
    second = g.forge("b2", version=0)
    assert first["bound"] is True
    assert second["bound"] is False
    state = g.snapshot()
    assert state["gears"]["b1"]["bound"] is True
    assert state["gears"]["b2"]["bound"] is False


def test_transfer_rejects_target_with_existing_level():
    g = gate({"src": base_gear(level=5), "dst": base_gear(level=2)},
             {"ore": 10}, roll=lambda: 0.1)
    with pytest.raises(ValueError):
        g.transfer("src", "dst", version=0)
    state = g.snapshot()
    assert state["gears"]["src"]["level"] == 5
    assert state["gears"]["dst"]["level"] == 2


def test_transfer_moves_level_and_zeroes_source():
    g = gate({"src": base_gear(level=5), "dst": base_gear(level=0)},
             {"ore": 10}, roll=lambda: 0.1)
    result = g.transfer("src", "dst", version=0)
    assert result["level"] == 5
    assert result["source_level"] == 0
    assert result["target_level"] == 5
    state = g.snapshot()
    assert state["gears"]["src"]["level"] == 0
    assert state["gears"]["dst"]["level"] == 5


def test_preview_next_stats_match_after_success():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.1)
    preview = g.preview("w")
    result = g.forge("w", version=0)
    assert preview["next_stats"] == {"attack": 60}
    assert result["stats"] == preview["next_stats"]


def test_restoring_older_snapshot_is_rejected():
    g = gate({"w": base_gear()}, {"ore": 10}, roll=lambda: 0.1)
    snapshot = g.snapshot()
    g.forge("w", version=0)
    with pytest.raises(ValueError):
        g.restore(snapshot)
