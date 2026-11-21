import pytest

import stereo_view_plan as svp


def locked_planner(**kwargs):
    planner = svp.StereoViewPlan(**kwargs)
    planner.submit_pose((0.0, 0.0, 0.0), yaw_deg=0.0)
    planner.lock_pose()
    return planner


def test_eyes_are_offset_by_ipd_around_head():
    result = locked_planner().plan()
    left, right = result["views"]
    assert left["position"][0] == pytest.approx(-0.032)
    assert right["position"][0] == pytest.approx(0.032)
    midpoint = (left["position"][0] + right["position"][0]) / 2.0
    assert midpoint == pytest.approx(0.0)


def test_projection_is_mirrored_between_eyes():
    result = locked_planner().plan()
    left = result["views"][0]["projection"]
    right = result["views"][1]["projection"]
    assert len(left) == 4 and all(len(row) == 4 for row in left)
    assert left[0][2] * right[0][2] < 0.0
    assert left[0][2] + right[0][2] == pytest.approx(0.0)
    assert left[2][2] == pytest.approx(right[2][2])
    assert left[1][1] == pytest.approx(right[1][1])


def test_instanced_view_order_is_fixed_left_then_right():
    result = locked_planner().plan()
    assert result["instanced_order"] == ["left", "right"]
    assert [view["eye"] for view in result["views"]] == ["left", "right"]


def test_cull_uses_union_of_both_eye_frusta():
    planner = locked_planner(width=1080, height=1080, fov_deg=60.0)
    assert planner.frustum_cull((5.77, 0.0, -10.0)) is True
    assert planner.frustum_cull((0.0, 0.0, -10.0)) is True


def test_point_outside_both_frusta_is_culled():
    planner = locked_planner(width=1080, height=1080, fov_deg=60.0)
    assert planner.frustum_cull((6.5, 0.0, -10.0)) is False
    assert planner.frustum_cull((0.0, 0.0, 1.0)) is False


def test_unlocked_pose_is_ignored():
    planner = svp.StereoViewPlan()
    assert planner.plan()["pose_used"]["position"] == pytest.approx((0.0, 0.0, 0.0))
    planner.submit_pose((5.0, 0.0, 0.0), yaw_deg=0.0)
    result = planner.plan()
    assert result["pose_used"]["position"][0] == pytest.approx(0.0)


def test_locked_pose_is_used_until_next_lock():
    planner = svp.StereoViewPlan()
    planner.submit_pose((5.0, 0.0, 0.0), yaw_deg=0.0)
    planner.lock_pose()
    result = planner.plan()
    assert result["pose_used"]["position"][0] == pytest.approx(5.0)
    midpoint = (result["views"][0]["position"][0] + result["views"][1]["position"][0]) / 2.0
    assert midpoint == pytest.approx(5.0)
    planner.submit_pose((9.0, 0.0, 0.0), yaw_deg=0.0)
    assert planner.plan()["pose_used"]["position"][0] == pytest.approx(5.0)


def test_reprojection_rejects_occluded_samples():
    planner = svp.StereoViewPlan()
    result = planner.reproject((0.5, 0.5), 5.0, (0.5, 0.5), 2.0)
    assert result["valid"] is False


def test_reprojection_accepts_consistent_samples():
    planner = svp.StereoViewPlan()
    result = planner.reproject((0.5, 0.5), 2.0, (0.4, 0.5), 2.05)
    assert result["valid"] is True


def test_invalid_ipd_falls_back_to_default():
    for bad_ipd in (0.0, -0.5):
        planner = svp.StereoViewPlan(ipd=bad_ipd)
        result = planner.plan()
        assert result["ipd_used"] == pytest.approx(planner.default_ipd)
        assert result["ipd_fallback"] is True
        separation = result["views"][1]["position"][0] - result["views"][0]["position"][0]
        assert separation == pytest.approx(planner.default_ipd)


def test_resolution_change_rebuilds_viewports():
    planner = locked_planner(width=1920, height=1080)
    result = planner.plan()
    assert tuple(result["views"][0]["viewport"]) == (0, 0, 960, 1080)
    assert tuple(result["views"][1]["viewport"]) == (960, 0, 960, 1080)
    planner.set_resolution(2560, 1440)
    result = planner.plan()
    assert tuple(result["views"][0]["viewport"]) == (0, 0, 1280, 1440)
    assert tuple(result["views"][1]["viewport"]) == (1280, 0, 1280, 1440)


def test_shadow_offsets_differ_per_eye():
    offsets = locked_planner().plan()["shadow_offsets"]
    assert len(offsets) == 2
    assert offsets[0][0] * offsets[1][0] < 0.0
    assert offsets[0][0] + offsets[1][0] == pytest.approx(0.0)
    assert offsets[0][1:] == pytest.approx(offsets[1][1:])


def test_over_budget_reports_degraded_scale():
    planner = locked_planner(budget_ms=11.0)
    normal = planner.plan()
    assert normal["degraded"] is False
    assert normal["target_scale"] == pytest.approx(1.0)
    over = planner.plan(elapsed_ms=12.0)
    assert over["degraded"] is True
    assert over["target_scale"] < 1.0


def test_plan_result_is_isolated_from_caller_mutation():
    planner = locked_planner()
    result = planner.plan()
    original = result["views"][0]["view"][0][3]
    result["views"][0]["view"][0][3] = 999.0
    again = planner.plan()
    assert again["views"][0]["view"][0][3] == pytest.approx(original)


def test_plan_reports_matrices_and_elapsed_time():
    planner = locked_planner()
    result = planner.plan(elapsed_ms=3.5)
    assert len(result["views"]) == 2
    for view in result["views"]:
        assert len(view["view"]) == 4
        assert all(len(row) == 4 for row in view["view"])
        assert all(len(row) == 4 for row in view["projection"])
        assert view["near"] == pytest.approx(planner.near)
        assert view["far"] == pytest.approx(planner.far)
    assert result["elapsed_ms"] == pytest.approx(3.5)
    assert "pose_used" in result
