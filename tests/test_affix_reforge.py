import pytest

import affix_reforge as ar


POOL = [
    {"id": "a1", "min_level": 1, "qualities": ["blue", "purple"], "set_id": "s1", "value": 10},
    {"id": "a2", "min_level": 1, "qualities": ["blue", "purple"], "set_id": "s1", "value": 12},
    {"id": "a3", "min_level": 1, "qualities": ["blue", "purple"], "set_id": None, "value": 8},
    {"id": "a4", "min_level": 1, "qualities": ["blue", "purple"], "set_id": None, "value": 9},
    {"id": "high", "min_level": 60, "qualities": ["blue", "purple"], "set_id": None, "value": 50},
    {"id": "epic", "min_level": 1, "qualities": ["purple"], "set_id": None, "value": 40},
]


def base_item(**over):
    data = {
        "level": 30,
        "quality": "blue",
        "slots": 3,
        "set_id": "s1",
        "version": 0,
        "affixes": [
            {"id": "a1", "value": 10, "set_id": "s1"},
            {"id": "a2", "value": 12, "set_id": "s1"},
            {"id": "a3", "value": 8, "set_id": None},
        ],
    }
    data.update(over)
    return data


def forge(items=None, inventory=None, roll=None):
    return ar.AffixReforge(
        pool=POOL,
        items=items or {"it1": base_item()},
        inventory=inventory or {"dust": 10},
        cost={"dust": 2},
        roll=roll or (lambda: 0.99),
    )


def ids(affixes):
    return [affix["id"] for affix in affixes]


def test_locked_affixes_are_unchanged():
    f = forge()
    f.lock("it1", ["a1", "a2"])
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    locked = [a for a in result["affixes"] if a["id"] in ("a1", "a2")]
    assert locked == base_item()["affixes"][:2]


def test_locked_affixes_are_not_in_candidate_pool():
    f = forge()
    f.lock("it1", ["a1", "a2"])
    preview = f.preview("it1")
    candidate_ids = ids(preview["candidates"])
    assert "a1" not in candidate_ids
    assert "a2" not in candidate_ids


def test_reforge_affix_ids_are_unique():
    f = forge()
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    result_ids = ids(result["affixes"])
    assert len(result_ids) == len(set(result_ids))


def test_reforge_preserves_slot_count():
    f = forge()
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    assert len(result["affixes"]) == 3


def test_low_level_item_cannot_roll_high_level_affix():
    f = forge()
    preview = f.preview("it1")
    assert "high" not in ids(preview["candidates"])
    result = f.reforge("it1", token=preview["token"])
    assert "high" not in ids(result["affixes"])


def test_blue_item_cannot_roll_purple_only_affix():
    f = forge()
    preview = f.preview("it1")
    assert "epic" not in ids(preview["candidates"])


def test_preview_candidates_match_reforge_result():
    f = forge()
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    assert result["affixes"] == preview["candidates"]


def test_preview_does_not_consume_materials():
    f = forge()
    first = f.preview("it1")
    second = f.preview("it1")
    assert first["cost"] == {"dust": 2}
    result = f.reforge("it1", token=second["token"])
    assert result["materials"]["dust"] == 8
    assert result["version"] == 1


def test_reused_token_cannot_double_charge():
    f = forge()
    preview = f.preview("it1")
    f.reforge("it1", token=preview["token"])
    with pytest.raises(ValueError):
        f.reforge("it1", token=preview["token"])
    second = f.preview("it1")
    result = f.reforge("it1", token=second["token"])
    assert result["materials"]["dust"] == 6


def test_history_keeps_every_attempt():
    f = forge()
    for _ in range(3):
        preview = f.preview("it1")
        f.reforge("it1", token=preview["token"])
    history = f.history("it1")
    assert len(history) == 3
    assert [entry["attempt"] for entry in history] == [1, 2, 3]


def test_undo_restores_affixes_and_refunds_materials():
    f = forge()
    original = [dict(a) for a in base_item()["affixes"]]
    preview = f.preview("it1")
    f.reforge("it1", token=preview["token"])
    result = f.undo("it1")
    assert result["affixes"] == original
    assert result["refunded"] == {"dust": 2}
    assert f.history("it1") == []


def test_set_bonus_is_recomputed_after_reforge():
    f = forge()
    f.lock("it1", ["a1", "a2"])
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    assert result["set_bonus"]["set_id"] == "s1"
    assert result["set_bonus"]["pieces"] == 2
    assert result["set_bonus"]["active"] is True


def test_stale_preview_token_cannot_overwrite_newer_reforge():
    f = forge()
    first = f.preview("it1")
    stale = f.preview("it1")
    applied = f.reforge("it1", token=first["token"])
    with pytest.raises(ValueError):
        f.reforge("it1", token=stale["token"])
    assert f.history("it1")[-1]["after"] == applied["affixes"]


def test_reforge_charges_materials_exactly_once():
    f = forge()
    preview = f.preview("it1")
    result = f.reforge("it1", token=preview["token"])
    assert result["cost"] == {"dust": 2}
    assert result["materials"]["dust"] == 8


def test_reforge_increments_item_version():
    f = forge()
    first = f.preview("it1")
    first_result = f.reforge("it1", token=first["token"])
    assert first_result["version"] == 1
    second = f.preview("it1")
    assert second["version"] == 1
    second_result = f.reforge("it1", token=second["token"])
    assert second_result["version"] == 2
