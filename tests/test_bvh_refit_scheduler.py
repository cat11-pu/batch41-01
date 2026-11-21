import pytest

import bvh_refit_scheduler as bvh


def tri(pid, translate=(0.0, 0.0, 5.0), verts=None):
    return {
        "id": pid,
        "verts": verts if verts is not None else [[-1.0, -1.0, 0.0],
                                                  [1.0, -1.0, 0.0],
                                                  [0.0, 1.0, 0.0]],
        "translate": list(translate),
    }


def test_build_rejects_non_finite_vertex():
    bad = tri("bad", verts=[[0.0, 0.0, float("nan")], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    with pytest.raises(ValueError):
        bvh.BvhRefitScheduler().build([bad])


def test_build_prunes_degenerate_triangle():
    degenerate = tri("deg", verts=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([degenerate])
    stats = scheduler.stats()
    assert stats["pruned"] == 1
    assert stats["primitive_count"] == 0
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == []


def test_query_hits_triangle_at_positive_distance():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    hits = scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert len(hits) == 1
    assert hits[0]["id"] == "a"
    assert hits[0]["t"] == pytest.approx(5.0)


def test_vertex_move_requires_refit_for_new_position():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))[0]["t"] == pytest.approx(5.0)
    scheduler.update_primitive("a", translate=(0.0, 0.0, 10.0))
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))[0]["t"] == pytest.approx(5.0)
    scheduler.refit()
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))[0]["t"] == pytest.approx(10.0)


def test_dirty_update_grows_root_bounds():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    scheduler.update_primitive("a", translate=(0.0, 0.0, 20.0))
    scheduler.refit()
    bounds = scheduler.root_bounds()
    assert bounds[1][2] >= 20.0 - 1e-9
    assert bounds[0][2] <= 20.0 + 1e-9


def test_old_revision_still_queries_old_tree():
    scheduler = bvh.BvhRefitScheduler()
    revision_one = scheduler.build([tri("a")])
    scheduler.update_primitive("a", translate=(0.0, 0.0, 10.0))
    revision_two = scheduler.refit()["revision"]
    assert revision_two > revision_one
    old = scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), revision=revision_one)
    new = scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert old[0]["t"] == pytest.approx(5.0)
    assert new[0]["t"] == pytest.approx(10.0)


def test_budget_overrun_prioritizes_high_priority_subtree():
    primitives = [tri(index, translate=(index * 3.0, 0.0, 5.0)) for index in range(4)]
    scheduler = bvh.BvhRefitScheduler(budget_ms=2.0)
    scheduler.build(primitives)
    for index in range(4):
        scheduler.update_primitive(index, translate=(index * 3.0, 0.0, 10.0))
        scheduler.mark_dirty(index, priority=index)
    result = scheduler.refit(elapsed_ms=9.0)
    assert result["degraded"] is True
    assert result["updated"] < result["dirty"]
    assert 3 in result["updated_ids"]
    assert result["skipped"] > 0


def test_removal_and_compaction_reclaim_primitives():
    primitives = [tri(index, translate=(index * 3.0, 0.0, 5.0)) for index in range(10)]
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build(primitives)
    for index in range(3):
        scheduler.remove(index)
    result = scheduler.compact()
    assert result["freed"] == 3
    assert result["remaining"] == 7
    assert scheduler.stats()["primitive_count"] == 7
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == []


def test_query_from_inside_root_bounds_ignores_negative_hits():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("front", (0.0, 0.0, 5.0)), tri("back", (0.0, 0.0, -5.0))])
    hits = scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert hits[0]["id"] == "front"
    assert hits[0]["t"] == pytest.approx(5.0)
    assert all(hit["t"] > 0.0 for hit in hits)


def test_instance_translation_marks_structure_dirty():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    assert scheduler.query((10.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == []
    scheduler.update_primitive("a", translate=(10.0, 0.0, 5.0))
    assert scheduler.query((10.0, 0.0, 0.0), (0.0, 0.0, 1.0)) == []
    scheduler.refit()
    hits = scheduler.query((10.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert hits[0]["id"] == "a"
    assert hits[0]["t"] == pytest.approx(5.0)


def test_bulk_update_reports_updated_node_count():
    primitives = [tri(index, translate=(index * 2.0, 0.0, 5.0)) for index in range(512)]
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build(primitives)
    for index in range(512):
        scheduler.update_primitive(index, translate=(index * 2.0, 0.0, 6.0))
    result = scheduler.refit()
    assert result["updated"] == 512
    assert result["degraded"] is False


def test_mark_dirty_unknown_primitive_is_rejected():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([])
    with pytest.raises(KeyError):
        scheduler.mark_dirty("missing")


def test_query_miss_returns_empty_list():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    assert scheduler.query((0.0, 0.0, 0.0), (0.0, 0.0, -1.0)) == []


def test_revision_increments_on_build_and_refit():
    scheduler = bvh.BvhRefitScheduler()
    first = scheduler.build([tri("a")])
    assert first >= 1
    assert scheduler.stats()["revision"] == first
    scheduler.mark_dirty("a")
    second = scheduler.refit()["revision"]
    assert second == first + 1


def test_refit_reports_counts_and_elapsed_time():
    scheduler = bvh.BvhRefitScheduler()
    scheduler.build([tri("a")])
    scheduler.mark_dirty("a")
    result = scheduler.refit(elapsed_ms=0.25)
    assert result["elapsed_ms"] == pytest.approx(0.25)
    assert result["updated"] == 1
    assert result["dirty"] == 1
    assert result["skipped"] == 0
    assert result["degraded"] is False
    stats = scheduler.stats()
    assert stats["primitive_count"] == 1
    assert stats["node_count"] >= 1
