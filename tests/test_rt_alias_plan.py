import threading

import pytest

import rt_alias_plan as rt


def basic_plan():
    planner = rt.RtAliasPlan()
    planner.add_resource("a", 1024)
    planner.add_resource("b", 1024)
    return planner


def test_overlapping_lifetimes_get_distinct_offsets():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    planner.add_pass("p2", writes=("a",), start=2, end=2)
    allocation = planner.plan()["allocation"]
    assert allocation["a"]["offset"] != allocation["b"]["offset"]


def test_disjoint_lifetimes_can_reuse_memory():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    result = planner.plan()
    assert result["allocation"]["a"]["offset"] == result["allocation"]["b"]["offset"]
    assert result["reuse_count"] >= 1


def test_peak_memory_matches_greedy_packing():
    planner = rt.RtAliasPlan()
    planner.add_resource("a", 1024)
    planner.add_resource("b", 1024)
    planner.add_resource("c", 1024)
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    planner.add_pass("p2", reads=("a",), writes=("a", "c"), start=2, end=2)
    assert planner.plan()["peak_bytes"] == 2048


def test_format_mismatch_prevents_aliasing():
    planner = rt.RtAliasPlan()
    planner.add_resource("a", 1024, fmt="rgba8")
    planner.add_resource("b", 1024, fmt="rgba16f")
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    allocation = planner.plan()["allocation"]
    assert allocation["a"]["offset"] != allocation["b"]["offset"]


def test_sample_count_mismatch_prevents_aliasing():
    planner = rt.RtAliasPlan()
    planner.add_resource("a", 1024, samples=1)
    planner.add_resource("b", 1024, samples=4)
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    allocation = planner.plan()["allocation"]
    assert allocation["a"]["offset"] != allocation["b"]["offset"]


def test_tile_mode_mismatch_prevents_aliasing():
    planner = rt.RtAliasPlan()
    planner.add_resource("a", 1024, tile="linear")
    planner.add_resource("b", 1024, tile="tiled")
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    allocation = planner.plan()["allocation"]
    assert allocation["a"]["offset"] != allocation["b"]["offset"]


def test_read_write_pass_cannot_alias_with_same_pass_resource():
    planner = basic_plan()
    planner.add_pass("p0", reads=("a",), writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=0, end=0)
    allocation = planner.plan()["allocation"]
    assert allocation["a"]["offset"] != allocation["b"]["offset"]


def test_barriers_are_emitted_between_producer_and_consumer():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    planner.add_pass("p2", reads=("a",), start=2, end=2)
    barriers = planner.plan()["barriers"]
    assert any(barrier["resource"] == "a" and barrier["from"] == 0 and barrier["to"] == 2
               for barrier in barriers)


def test_inflight_resource_is_protected():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    normal = planner.plan()["allocation"]
    assert normal["a"]["offset"] == normal["b"]["offset"]
    protected = planner.plan(inflight={"a"})["allocation"]
    assert protected["a"]["offset"] != protected["b"]["offset"]


def test_external_resource_is_excluded_from_pool():
    planner = rt.RtAliasPlan()
    planner.add_resource("ui", 512, external=True)
    planner.add_pass("ui_pass", writes=("ui",), start=0, end=0)
    result = planner.plan()
    assert result["allocation"]["ui"]["offset"] is None
    assert result["allocation"]["ui"]["external"] is True
    assert result["peak_bytes"] == 0


def test_zero_size_resource_is_rejected():
    planner = rt.RtAliasPlan()
    with pytest.raises(ValueError):
        planner.add_resource("zero", 0)


def test_oversized_resource_is_rejected():
    planner = rt.RtAliasPlan(max_size=1024)
    with pytest.raises(ValueError):
        planner.add_resource("huge", 2048)


def test_resolution_change_recomputes_plan():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    first = planner.plan()
    planner.set_resolution(2560, 1440)
    second = planner.plan()
    assert second["revision"] == first["revision"] + 1
    assert list(second["resolution"]) == [2560, 1440]


def test_concurrent_plan_calls_are_serialized():
    planner = basic_plan()
    planner.add_pass("p0", writes=("a",), start=0, end=0)
    planner.add_pass("p1", writes=("b",), start=1, end=1)
    results = []
    errors = []

    def run():
        try:
            results.append(planner.plan())
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(results) == 4
    assert all(result["peak_bytes"] == results[0]["peak_bytes"] for result in results)
    offsets = {result["allocation"]["a"]["offset"] for result in results}
    assert len(offsets) == 1


def test_allocation_table_reports_reuse_and_alignment():
    planner = rt.RtAliasPlan()
    for rid in ("a", "b", "c", "d"):
        planner.add_resource(rid, 1024)
    for index, rid in enumerate(("a", "b", "c", "d")):
        planner.add_pass("p%d" % index, writes=(rid,), start=index, end=index)
    result = planner.plan()
    assert result["peak_bytes"] == 1024
    assert result["reuse_count"] == 3
    assert set(result["allocation"]["a"]["aliases"]) == {"b", "c", "d"}
    for entry in result["allocation"].values():
        assert entry["offset"] % 64 == 0
