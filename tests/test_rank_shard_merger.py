import pytest

import rank_shard_merger as rsm


def merger():
    return rsm.RankShardMerger(max_page_size=100)


def test_same_character_across_shards_is_merged_once():
    m = merger()
    m.submit("S1", "s1", "c1", 100, 1, 1)
    m.submit("S1", "s2", "c1", 120, 2, 1)
    entries = m.rank("S1")["entries"]
    assert len(entries) == 1
    assert entries[0]["character_id"] == "c1"
    assert entries[0]["score"] == 120
    assert entries[0]["rank"] == 1


def test_distinct_characters_are_not_collapsed():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    m.submit("S1", "s2", "c2", 20, 1, 1)
    ids = [e["character_id"] for e in m.rank("S1")["entries"]]
    assert sorted(ids) == ["c1", "c2"]


def test_merge_waits_for_shard_watermark():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 4)
    with pytest.raises(ValueError):
        m.merge("S1", {"s1": 5})
    assert m.merge("S1", {"s1": 4})["merged"] == 1


def test_merge_uses_latest_shard_score():
    m = merger()
    m.submit("S1", "s1", "c1", 50, 1, 1)
    m.submit("S1", "s2", "c1", 70, 2, 2)
    result = m.merge("S1", {"s1": 1, "s2": 2})
    assert result["merged"] == 1
    assert m.rank("S1")["entries"][0]["score"] == 70


def test_page_cursor_has_no_duplicates_or_gaps():
    m = merger()
    for index in range(5):
        m.submit("S1", "s1", "c%d" % (index + 1), 50 - index * 10, 1, 1)
    expected = [e["character_id"] for e in m.rank("S1")["entries"]]
    seen = []
    cursor = None
    while True:
        page = m.page("S1", cursor=cursor, size=2)
        seen.extend(e["character_id"] for e in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == expected
    assert len(seen) == len(set(seen)) == 5


def test_page_size_is_clamped():
    m = merger()
    for index in range(120):
        m.submit("S1", "s1", "c%03d" % index, 1000 - index, 1, 1)
    page = m.page("S1", size=500)
    assert len(page["items"]) == 100
    assert page["size"] == 100
    assert page["next_cursor"] is not None


def test_submit_invalidates_rank_cache():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    first = m.rank("S1")
    m.submit("S1", "s1", "c2", 20, 1, 1)
    second = m.rank("S1")
    assert second["version"] != first["version"]
    assert "c2" in [e["character_id"] for e in second["entries"]]


def test_invalidate_bumps_cache_version():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    m.rank("S1")
    version = m.invalidate("S1")
    assert version > 0
    assert m.rank("S1")["version"] == version


def test_stale_write_version_is_rejected():
    m = merger()
    m.submit("S1", "s1", "c1", 200, 5, 1)
    stale = m.submit("S1", "s1", "c1", 300, 4, 2)
    assert stale["accepted"] is False
    assert stale["reason"] == "stale"
    assert m.rank("S1")["entries"][0]["score"] == 200


def test_merge_is_idempotent():
    m = merger()
    m.submit("S1", "s1", "c1", 30, 1, 1)
    m.submit("S1", "s2", "c2", 20, 1, 1)
    m.merge("S1")
    first = m.rank("S1")["entries"]
    m.merge("S1")
    second = m.rank("S1")["entries"]
    assert first == second
    assert len(second) == 2


def test_seasons_are_isolated():
    m = merger()
    m.submit("preseason", "s1", "c0", 99, 1, 1)
    m.submit("S1", "s1", "c1", 10, 1, 1)
    ids = [e["character_id"] for e in m.rank("S1")["entries"]]
    assert ids == ["c1"]


def test_expansion_keeps_existing_shard_data():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    m.merge("S1")
    m.submit("S1", "s2", "c2", 20, 1, 1)
    result = m.merge("S1")
    ids = sorted(e["character_id"] for e in m.rank("S1")["entries"])
    assert ids == ["c1", "c2"]
    assert result["shards"] == ["s1", "s2"]


def test_removed_character_does_not_return_after_merge():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    m.merge("S1")
    m.remove("S1", "c1")
    assert m.rank("S1")["entries"] == []
    m.merge("S1")
    assert m.rank("S1")["entries"] == []


def test_rank_sorting_breaks_ties_by_character_id():
    m = merger()
    m.submit("S1", "s1", "c2", 100, 1, 1)
    m.submit("S1", "s1", "c1", 100, 1, 1)
    m.submit("S1", "s1", "c3", 120, 1, 1)
    entries = m.rank("S1")["entries"]
    assert [e["character_id"] for e in entries] == ["c3", "c1", "c2"]
    assert [e["rank"] for e in entries] == [1, 2, 3]


def test_unknown_page_cursor_is_rejected():
    m = merger()
    m.submit("S1", "s1", "c1", 10, 1, 1)
    with pytest.raises(ValueError):
        m.page("S1", cursor="missing")
