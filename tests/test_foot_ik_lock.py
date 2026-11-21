import pytest

import foot_ik_lock as fik


def flat(x, z):
    return (0.0, (0.0, 1.0, 0.0))


def slope30(x, z):
    return (0.0, (0.0, 0.8660254, 0.5))


def foot(x=0.0, y=0.0, z=0.0, toe_y=None, heel_y=None):
    if toe_y is None:
        toe_y = y
    if heel_y is None:
        heel_y = y
    return {"ankle": (x, y, z), "toe": (x, toe_y, z + 0.1), "heel": (x, heel_y, z - 0.1)}


def character(x=0.0, y=0.0, z=0.0, yaw_deg=0.0):
    return {"position": (x, y, z), "yaw_deg": yaw_deg}


def pair(left=None, right=None):
    return {"left": left or foot(-0.2), "right": right or foot(0.2)}


def test_flat_ground_needs_no_offset():
    result = fik.FootIkLock().solve(pair(), character(), flat)
    assert abs(result["ankle_offsets"]["left"][1]) < 1e-6
    assert abs(result["ankle_offsets"]["right"][1]) < 1e-6


def test_toe_below_ground_lifts_the_foot():
    result = fik.FootIkLock().solve(
        pair(foot(-0.2, y=0.1, toe_y=-0.05, heel_y=0.05)), character(), flat
    )
    assert result["ankle_offsets"]["left"][1] == pytest.approx(0.05)


def test_slope_normal_sets_foot_pitch():
    result = fik.FootIkLock().solve(pair(), character(), slope30)
    assert result["foot_pitch_deg"]["left"] == pytest.approx(30.0, abs=1.0)


def test_pelvis_drops_to_lowest_foot():
    def stepped(x, z):
        if x < 0:
            return (0.0, (0.0, 1.0, 0.0))
        return (-0.2, (0.0, 1.0, 0.0))

    result = fik.FootIkLock(pelvis_max_drop=0.4).solve(pair(), character(), stepped)
    assert result["pelvis_drop"] == pytest.approx(0.2, abs=1e-6)


def test_pelvis_drop_is_clamped():
    def deep(x, z):
        if x < 0:
            return (0.0, (0.0, 1.0, 0.0))
        return (-2.0, (0.0, 1.0, 0.0))

    result = fik.FootIkLock(pelvis_max_drop=0.4).solve(pair(), character(), deep)
    assert result["pelvis_drop"] <= 0.4


def test_foot_lock_is_stored_in_character_local_space():
    lock = fik.FootIkLock()
    first = lock.solve({"left": foot(0.2, 0.0, 0.0), "right": foot(0.6, 0.0, 0.0)},
                       character(0.0, 0.0, 0.0, 0.0), flat)
    second = lock.solve({"left": foot(10.2, 0.0, 0.0), "right": foot(10.6, 0.0, 0.0)},
                        character(10.0, 0.0, 0.0, 0.0), flat)
    assert first["locks"]["left"]["local"][0] == pytest.approx(0.2)
    assert second["locks"]["left"]["local"][0] == pytest.approx(0.2)


def test_side_facing_surface_is_not_used_as_floor():
    def wall(x, z):
        return (0.0, (1.0, 0.0, 0.0))

    result = fik.FootIkLock().solve(pair(), character(), wall)
    assert result["surfaces"]["left"]["accepted"] is False
    assert result["ankle_offsets"]["left"][1] == 0.0


def test_knee_angle_stays_in_valid_range():
    result = fik.FootIkLock(leg_length=1.0).solve(pair(), character(), flat)
    assert 0.0 <= result["knee_angle_deg"]["left"] <= 180.0
    assert result["knee_inverted"]["left"] is False


def test_target_beyond_leg_length_releases_weight_gradually():
    far = {"left": foot(3.0, 0.0, 0.0), "right": foot(0.2, 0.0, 0.0)}
    result = fik.FootIkLock(leg_length=1.0).solve(far, character(), flat)
    assert 0.0 < result["weights"]["left"] < 1.0


def test_slope_change_is_smoothed_over_time():
    lock = fik.FootIkLock(slope_smooth_deg_per_s=90.0)
    lock.solve(pair(), character(), flat, dt=1.0 / 30.0)
    result = lock.solve(pair(), character(), slope30, dt=1.0 / 30.0)
    assert result["foot_pitch_deg"]["left"] <= 3.01


def test_hit_releases_only_the_hit_foot():
    lock = fik.FootIkLock()
    lock.solve(pair(), character(), flat)
    lock.release("left", reason="hit")
    stats = lock.stats()
    assert stats["locked"]["left"] is False
    assert stats["locked"]["right"] is True


def test_death_releases_all_locks():
    lock = fik.FootIkLock()
    lock.solve(pair(), character(), flat)
    lock.release(None, reason="death")
    stats = lock.stats()
    assert stats["locked"]["left"] is False
    assert stats["locked"]["right"] is False


def test_nan_target_is_rejected():
    bad = pair(foot(-0.2, y=float("nan")))
    with pytest.raises(ValueError):
        fik.FootIkLock().solve(bad, character(), flat)


def test_zero_leg_length_is_rejected():
    with pytest.raises(ValueError):
        fik.FootIkLock(leg_length=0.0)


def test_pelvis_is_written_atomically_once():
    lock = fik.FootIkLock()
    result = lock.solve(pair(), character(), flat)
    assert result["pelvis"]["drop"] == pytest.approx(result["pelvis_drop"])
    assert lock.stats()["pelvis_writes"] == 1
    assert result["elapsed_ms"] >= 0
