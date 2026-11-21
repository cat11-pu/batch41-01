import pytest

import motion_match_index as mm


def clip(clip_id, speed=0.0, facing=0.0, angular=0.0, length=1.0, mirror=False):
    return {
        "id": clip_id,
        "features": {"speed": speed, "facing": facing, "angular": angular},
        "length": length,
        "mirror": mirror,
    }


def query(facing=0.0, angular=0.0, speed=0.0):
    return {"speed": speed, "facing": facing, "angular": angular}


def test_features_are_normalized_per_dimension():
    index = mm.MotionMatchIndex()
    index.build([
        clip("steep", speed=1.0, facing=0.0),
        clip("wide", speed=101.0, facing=0.0),
    ])
    results = index.query(query(speed=71.0, facing=0.0), limit=2)
    costs = {entry["id"]: entry["cost"] for entry in results}
    assert costs["wide"] == pytest.approx(0.3)
    assert costs["steep"] == pytest.approx(0.7)
    assert results[0]["id"] == "wide"


def test_removed_clip_is_not_returned():
    index = mm.MotionMatchIndex()
    index.build([clip("a"), clip("b", facing=1.0)])
    index.remove("b")
    ids = [entry["id"] for entry in index.query(query(facing=1.0), limit=5)]
    assert "b" not in ids
    assert ids == ["a"]


def test_mirror_clip_flips_lateral_features():
    index = mm.MotionMatchIndex()
    index.build([
        clip("right", speed=0.0, facing=-1.0, angular=-0.5, mirror=True),
        clip("left", speed=0.0, facing=-1.0, angular=-0.5, mirror=False),
    ])
    result = index.query(query(facing=1.0, angular=0.5), limit=1)[0]
    assert result["id"] == "right"
    assert result["mirror"] is True
    assert result["cost"] == pytest.approx(0.0, abs=1e-9)


def test_hysteresis_keeps_current_on_small_gain():
    index = mm.MotionMatchIndex(hysteresis=0.5)
    index.build([clip("a", angular=0.0), clip("b", angular=1.0)])
    results = index.query(query(angular=0.55), current="a")
    assert len(results) == 1
    assert results[0]["id"] == "a"
    assert results[0]["sticky"] is True


def test_hysteresis_switches_on_clear_gain():
    index = mm.MotionMatchIndex(hysteresis=0.5)
    index.build([clip("a", angular=0.0), clip("b", angular=1.0)])
    results = index.query(query(angular=0.9), current="a")
    assert results[0]["id"] == "b"
    assert results[0]["sticky"] is False


def test_current_clip_is_excluded():
    index = mm.MotionMatchIndex()
    index.build([clip("a", angular=0.0), clip("b", angular=0.1)])
    ids = [entry["id"] for entry in index.query(query(angular=0.1), current="b", limit=5)]
    assert "b" not in ids
    assert ids == ["a"]


def test_empty_library_returns_empty():
    index = mm.MotionMatchIndex()
    index.build([])
    assert index.query(query(), limit=5) == []


def test_single_clip_library_returns_clip():
    index = mm.MotionMatchIndex()
    index.build([clip("only", angular=0.3)])
    results = index.query(query(angular=0.3))
    assert results[0]["id"] == "only"
    assert results[0]["cost"] == pytest.approx(0.0)


def test_single_clip_with_current_returns_empty():
    index = mm.MotionMatchIndex()
    index.build([clip("only", angular=0.3)])
    assert index.query(query(angular=0.3), current="only") == []


def test_candidate_weight_follows_length():
    index = mm.MotionMatchIndex()
    index.build([
        clip("short", angular=0.0, length=0.5),
        clip("long", angular=1.0, length=2.0),
    ])
    results = index.query(query(angular=0.0), limit=5, query_length=1.0)
    weights = {entry["id"]: entry["weight"] for entry in results}
    assert weights["short"] == pytest.approx(1.0)
    assert weights["long"] == pytest.approx(0.5)


def test_query_version_isolation():
    index = mm.MotionMatchIndex()
    index.build([clip("a", angular=0.0)])
    old_version = index.version()
    index.insert(clip("b", angular=0.1))
    old_results = index.query(query(angular=0.1), version=old_version)
    new_results = index.query(query(angular=0.1))
    assert [entry["id"] for entry in old_results] == ["a"]
    assert "b" in [entry["id"] for entry in new_results]


def test_result_contains_cost_and_id():
    index = mm.MotionMatchIndex()
    index.build([clip("a", angular=0.3)])
    entry = index.query(query(angular=0.3))[0]
    assert set(entry) == {"id", "cost", "weight", "mirror", "sticky"}
    assert entry["id"] == "a"
    assert isinstance(entry["cost"], float)


def test_large_library_build_and_query():
    index = mm.MotionMatchIndex()
    clips = [
        clip("clip-%d" % i, speed=float(i % 50), facing=(i % 10) / 10.0, angular=(i % 7) / 7.0)
        for i in range(1000)
    ]
    index.build(clips)
    results = index.query(query(speed=10.0, facing=0.5, angular=0.5), limit=1)
    assert len(results) == 1
    assert index.stats()["clips"] == 1000


def test_hard_stop_prediction_uses_current_speed():
    index = mm.MotionMatchIndex()
    stopped = index.predict(query(speed=0.0), horizon=0.2, previous_speed=5.0)
    moving = index.predict(query(speed=3.0), horizon=0.5)
    assert stopped["distance"] == pytest.approx(0.0)
    assert moving["distance"] == pytest.approx(1.5)


def test_stats_report_version_and_candidates():
    index = mm.MotionMatchIndex()
    index.build([clip("a"), clip("b", angular=1.0)])
    assert index.stats()["clips"] == 2
    assert index.stats()["version"] == 1
    index.remove("a")
    stats = index.stats()
    assert stats["clips"] == 1
    assert stats["version"] == 2
