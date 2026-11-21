import math

import pytest

import particle_collision_field as pcf


STONE = {"friction": 0.0, "restitution": 0.0}
BOUNCY = {"friction": 0.0, "restitution": 1.0}
STOP = {"friction": 1.0, "restitution": 0.0}


def floor_triangle():
    return (-10.0, 0.0, -10.0), (0.0, 0.0, 10.0), (10.0, 0.0, -10.0)


def wall_triangle():
    return (-10.0, 0.0, 0.0), (10.0, 0.0, 0.0), (-10.0, 10.0, 0.0)


def test_fast_particle_does_not_tunnel_through_thin_floor():
    field = pcf.ParticleCollisionField(materials={"stone": STONE})
    field.add_triangle(*floor_triangle(), material="stone")
    field.add_particle("p", (0.0, 2.0, 0.0), (0.0, -200.0, 0.0), 0.5,
                       material="stone")
    result = field.step(1.0)
    assert result["substeps"] > 1
    position = result["positions"]["p"]
    assert position[1] >= 0.49
    assert abs(result["velocities"]["p"][1]) <= 1e-6
    assert all(math.isfinite(value) for value in position)


def test_substep_count_scales_with_particle_speed():
    slow = pcf.ParticleCollisionField()
    slow.add_particle("p", (0.0, 50.0, 0.0), (0.0, -1.0, 0.0), 0.5)
    fast = pcf.ParticleCollisionField()
    fast.add_particle("p", (0.0, 50.0, 0.0), (0.0, -8.0, 0.0), 0.5)
    slow_steps = slow.step(1.0)["substeps"]
    fast_steps = fast.step(1.0)["substeps"]
    assert fast_steps > slow_steps
    assert fast_steps >= 2


def test_cell_size_tracks_largest_particle_radius():
    field = pcf.ParticleCollisionField()
    field.add_particle("small", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.1)
    field.add_particle("big", (10.0, 0.0, 0.0), (0.0, 0.0, 0.0), 3.0)
    assert field.cell_size >= 6.0 - 1e-9


def test_explicit_small_cell_size_is_clamped():
    field = pcf.ParticleCollisionField(cell_size=0.1, max_radius=2.0)
    assert field.cell_size >= 4.0 - 1e-9


def test_normal_at_shared_edge_is_smoothed():
    field = pcf.ParticleCollisionField()
    field.add_triangle((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
    field.add_triangle((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    normal = field.normal_at(0, (0.5, 0.0, 0.0))
    scale = 1.0 / math.sqrt(2.0)
    assert normal == pytest.approx((0.0, scale, scale), abs=1e-6)


def test_missing_material_falls_back_to_defaults():
    field = pcf.ParticleCollisionField(materials={})
    field.add_triangle(*floor_triangle(), material="ghost")
    field.add_particle("p", (0.0, 1.0, 0.0), (0.0, -10.0, 0.0), 0.5,
                       material="ghost")
    result = field.step(0.5)
    assert result["fallback_materials"] >= 1
    assert result["pairs"] >= 1
    assert all(math.isfinite(value) for value in result["velocities"]["p"])
    assert all(math.isfinite(value) for value in result["positions"]["p"])


def test_removed_particle_leaves_the_hash():
    field = pcf.ParticleCollisionField()
    field.add_particle("ghost", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.5)
    field.remove_particle("ghost")
    assert field.stats()["hash_particles"] == 0
    field.add_particle("alive", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.5)
    assert field.step(0.1)["pairs"] == 0


def test_single_contact_reflects_velocity_once():
    field = pcf.ParticleCollisionField(materials={"bouncy": BOUNCY})
    field.add_triangle(*floor_triangle(), material="bouncy")
    field.add_particle("p", (0.0, 0.45, 0.0), (0.0, -2.0, 0.0), 0.4,
                       material="bouncy")
    result = field.step(0.1)
    assert result["pairs"] == 1
    velocity_y = result["velocities"]["p"][1]
    assert velocity_y > 0.0
    assert velocity_y == pytest.approx(2.0, abs=0.2)


def test_corner_contact_does_not_ping_pong():
    field = pcf.ParticleCollisionField(materials={"stop": STOP})
    field.add_triangle(*floor_triangle(), material="stop")
    field.add_triangle(*wall_triangle(), material="stop")
    field.add_particle("p", (0.0, 0.2, 0.2), (0.0, -1.0, -1.0), 0.3,
                       material="stop")
    result = field.step(0.05)
    position = result["positions"]["p"]
    assert position[1] >= 0.3 - 1e-6
    assert position[2] >= 0.3 - 1e-6
    assert all(math.isfinite(value) for value in position)


def build_pair_field():
    field = pcf.ParticleCollisionField()
    for i in range(4):
        field.add_particle("a%d" % i, (i * 3.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.6)
        field.add_particle("b%d" % i, (i * 3.0 + 0.5, 0.0, 0.0), (0.0, 0.0, 0.0), 0.6)
    return field


def test_parallel_workers_match_single_worker():
    single = build_pair_field().step(0.1, workers=1)
    parallel = build_pair_field().step(0.1, workers=4)
    assert parallel["pairs"] == single["pairs"]
    for pid, position in single["positions"].items():
        assert parallel["positions"][pid] == pytest.approx(position, abs=1e-9)
    for pid, velocity in single["velocities"].items():
        assert parallel["velocities"][pid] == pytest.approx(velocity, abs=1e-9)


def test_budget_overrun_degrades_by_priority():
    field = pcf.ParticleCollisionField(budget_ms=1.0)
    for i in range(4):
        field.add_particle(i, (i * 2.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.6,
                           priority=i)
        field.add_particle(10 + i, (i * 2.0 + 0.4, 0.0, 0.0), (0.0, 0.0, 0.0),
                           0.6, priority=i)
    result = field.step(0.1, elapsed_ms=5.0)
    assert result["degraded"] is True
    assert result["processed"] < result["candidates"]
    assert result["skipped_ids"]
    assert (3 in result["processed_ids"]) or (13 in result["processed_ids"])
    for pid in result["skipped_ids"]:
        assert (pid % 10) < 3


def test_negative_radius_is_rejected():
    field = pcf.ParticleCollisionField()
    with pytest.raises(ValueError):
        field.add_particle("bad", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), -0.1)
    with pytest.raises(ValueError):
        field.add_particle("zero", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0)


def test_non_finite_position_is_rejected():
    field = pcf.ParticleCollisionField()
    with pytest.raises(ValueError):
        field.add_particle("bad", (float("nan"), 0.0, 0.0), (0.0, 0.0, 0.0), 0.5)
    with pytest.raises(ValueError):
        field.add_particle("bad", (0.0, 0.0, 0.0), (float("inf"), 0.0, 0.0), 0.5)


def build_shared_edge_field():
    field = pcf.ParticleCollisionField(materials={"stop": STOP})
    field.add_triangle((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0),
                       material="stop")
    field.add_triangle((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0),
                       material="stop")
    field.add_particle("p", (0.0, 0.45, 0.5), (0.0, -2.0, 0.0), 0.4,
                       material="stop")
    return field


def test_particle_on_shared_edge_has_stable_ownership():
    first = build_shared_edge_field().step(0.1)
    second = build_shared_edge_field().step(0.1)
    assert first["pairs"] == 1
    assert second["pairs"] == 1
    assert first["positions"]["p"] == pytest.approx(second["positions"]["p"])
    assert first["velocities"]["p"] == pytest.approx(second["velocities"]["p"])


def test_step_reports_counts_and_elapsed_time():
    field = pcf.ParticleCollisionField(materials={"stone": STONE})
    field.add_triangle(*floor_triangle(), material="stone")
    field.add_particle("p", (0.0, 1.0, 0.0), (0.0, -5.0, 0.0), 0.5,
                       material="stone")
    result = field.step(0.2, elapsed_ms=0.75)
    assert result["elapsed_ms"] == pytest.approx(0.75)
    assert result["substeps"] >= 1
    assert result["pairs"] == 1
    stats = field.stats()
    assert stats["triangles"] == 1
    assert stats["particles"] == 1
    assert stats["hash_particles"] == 1
