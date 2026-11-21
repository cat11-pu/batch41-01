import pytest

import expression_retarget as er


def rig(mapping=None, mirrored=False):
    return er.ExpressionRetarget(
        ["jaw_open", "smile", "blink", "smile_left"],
        ["mouth_open", "mouth_corner", "eye_close", "smile_right"],
        mapping=mapping,
        mirrored=mirrored,
    )


def test_semantic_mapping_routes_jaw_to_mouth():
    e = rig(mapping={"jaw_open": "mouth_open", "smile": "mouth_corner"})
    result = e.retarget({"weights": {"jaw_open": 1.0}})
    assert result["weights"]["mouth_open"] == pytest.approx(1.0)
    assert result["weights"]["mouth_corner"] == pytest.approx(0.0)


def test_unmapped_shape_uses_identity():
    e = er.ExpressionRetarget(["blink"], ["blink"])
    result = e.retarget({"weights": {"blink": 0.7}})
    assert result["weights"]["blink"] == pytest.approx(0.7)


def test_normalized_mode_sums_each_layer_to_one():
    e = rig()
    result = e.blend({"mouth_open": 2.0, "mouth_corner": 2.0}, {"eye_close": 1.0},
                     mode="normalized")
    assert result["layers"]["expression"]["mouth_open"] == pytest.approx(0.5)
    assert result["layers"]["viseme"]["eye_close"] == pytest.approx(1.0)
    assert sum(result["layers"]["expression"].values()) == pytest.approx(1.0)


def test_raw_mode_preserves_weights_without_global_clamp():
    e = rig()
    result = e.blend({"mouth_open": 2.0, "mouth_corner": 2.0}, {"eye_close": 1.0},
                     mode="raw")
    assert result["weights"]["mouth_open"] == pytest.approx(2.0)
    assert result["weights"]["mouth_corner"] == pytest.approx(2.0)
    assert result["total"] == pytest.approx(5.0)


def test_layer_blend_keeps_total_instead_of_clamping():
    e = rig()
    result = e.blend({"mouth_open": 1.0}, {"mouth_open": 0.5}, mode="raw")
    assert result["weights"]["mouth_open"] == pytest.approx(1.5)
    assert result["total"] == pytest.approx(1.5)


def test_missing_target_shape_is_warned_not_silent():
    e = er.ExpressionRetarget(["jaw_open", "smile"], ["mouth_open"],
                              mapping={"jaw_open": "mouth_open", "smile": "mouth_corner"})
    result = e.retarget({"weights": {"jaw_open": 1.0, "smile": 1.0}})
    assert "mouth_corner" in result["warnings"]
    assert result["weights"]["mouth_open"] == pytest.approx(1.0)


def test_unknown_source_shape_is_rejected():
    e = rig()
    with pytest.raises(ValueError):
        e.retarget({"weights": {"ghost": 1.0}})


def test_empty_frame_is_rejected():
    e = rig()
    with pytest.raises(ValueError):
        e.retarget({})


def test_all_zero_weights_are_handled():
    e = er.ExpressionRetarget(["jaw_open"], ["mouth_open"],
                              mapping={"jaw_open": "mouth_open"})
    result = e.retarget({"weights": {"jaw_open": 0.0}})
    assert result["weights"]["mouth_open"] == pytest.approx(0.0)
    assert result["warnings"] == []


def test_mirrored_rig_flips_left_right_shapes():
    e = er.ExpressionRetarget(["smile_left"], ["smile_right"],
                              mapping={"smile_left": "smile_left"}, mirrored=True)
    result = e.retarget({"weights": {"smile_left": 1.0}})
    assert result["weights"]["smile_right"] == pytest.approx(1.0)


def test_head_pose_is_decoupled_from_expression():
    e = rig(mapping={"jaw_open": "mouth_open"})
    result = e.retarget({"weights": {"jaw_open": 1.0}, "head": {"yaw": 15.0}})
    assert result["head"]["yaw"] == pytest.approx(15.0)
    assert result["weights"]["mouth_open"] == pytest.approx(1.0)


def test_low_fps_frames_are_interpolated():
    e = er.ExpressionRetarget(["a"], ["a"])
    result = e.retarget_at({"weights": {"a": 0.0}}, 0,
                           {"weights": {"a": 1.0}}, 100, 50)
    assert result["weights"]["a"] == pytest.approx(0.5)


def test_snapshot_is_a_deep_copy():
    e = rig()
    e.retarget({"weights": {"jaw_open": 1.0}})
    snap = e.snapshot()
    snap["weights"]["mouth_open"] = 99.0
    assert e.snapshot()["weights"]["mouth_open"] != 99.0


def test_missing_shape_is_not_added_to_weight_table():
    e = er.ExpressionRetarget(["jaw_open", "smile"], ["mouth_open"],
                              mapping={"jaw_open": "mouth_open", "smile": "mouth_corner"})
    result = e.retarget({"weights": {"jaw_open": 0.6, "smile": 0.4}})
    assert result["weights"]["mouth_open"] == pytest.approx(0.6)
    assert "mouth_corner" not in result["weights"]


def test_retarget_reports_layers_and_elapsed():
    e = rig(mapping={"jaw_open": "mouth_open"})
    result = e.retarget({"weights": {"jaw_open": 1.0}})
    assert set(result["layers"]) == {"expression", "viseme"}
    assert result["elapsed_ms"] >= 0
    assert e.stats()["frames"] == 1
