import threading

import pytest

import card_star_up as cs


def build(rng=None):
    service = cs.CardStarUp(rng=rng)
    service.add_card("hero", "tpl_hero", "purple", star=1, level=7)
    return service


def test_preview_matches_settlement_bonus():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    preview = service.preview("hero", ["mat"])
    result = service.star_up("hero", ["mat"])
    assert preview["star"] == 2
    assert result["attack_bonus_percent"] == preview["attack_bonus_percent"]
    assert result["attack_bonus_percent"] > 0


def test_star_up_keeps_level():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    service.star_up("hero", ["mat"])
    state = service.card_state("hero")
    assert state["star"] == 2
    assert state["level"] == 7


def test_star_up_recomputes_skill_points():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    service.star_up("hero", ["mat"])
    assert service.card_state("hero")["skill_points"] == 11


def test_duplicate_material_ids_are_consumed_once():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    result = service.star_up("hero", ["mat", "mat"])
    assert result["consumed"] == ["mat"]
    assert service.card_state("mat")["in_bag"] is False
    assert service.card_state("hero")["star"] == 2


def test_max_star_upgrade_is_rejected():
    service = cs.CardStarUp()
    service.add_card("hero", "tpl_hero", "purple", star=6, level=1)
    service.add_card("mat", "tpl_hero", "purple")
    with pytest.raises(ValueError):
        service.star_up("hero", ["mat"])
    assert service.card_state("hero")["star"] == 6
    assert service.card_state("mat")["in_bag"] is True


def test_quality_mismatch_is_rejected():
    service = build()
    service.add_card("mat", "tpl_hero", "white")
    with pytest.raises(ValueError):
        service.star_up("hero", ["mat"])
    assert service.card_state("mat")["in_bag"] is True


def test_different_template_is_rejected():
    service = build()
    service.add_card("mat", "tpl_other", "purple")
    with pytest.raises(ValueError):
        service.star_up("hero", ["mat"])
    assert service.card_state("mat")["in_bag"] is True


def test_material_count_must_equal_star():
    service = cs.CardStarUp()
    service.add_card("hero", "tpl_hero", "purple", star=2, level=3)
    service.add_card("mat", "tpl_hero", "purple")
    with pytest.raises(ValueError):
        service.star_up("hero", ["mat"])
    assert service.card_state("hero")["star"] == 2


def test_consumed_material_cannot_be_reused():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    service.add_card("other", "tpl_hero", "purple")
    service.star_up("hero", ["mat"])
    with pytest.raises(ValueError):
        service.star_up("other", ["mat"])
    assert service.card_state("other")["star"] == 1


def test_decompose_refunds_actual_investment():
    service = build()
    service.add_card("mat", "tpl_hero", "purple", star=2)
    service.star_up("hero", ["mat"])
    refund = service.decompose("hero")
    assert refund["refund_stars"] == [2]
    assert refund["shards"] == 20


def test_locked_card_keeps_lock_and_cannot_be_decomposed():
    service = cs.CardStarUp()
    service.add_card("hero", "tpl_hero", "purple", locked=True)
    service.add_card("mat", "tpl_hero", "purple")
    service.star_up("hero", ["mat"])
    assert service.card_state("hero")["locked"] is True
    with pytest.raises(ValueError):
        service.decompose("hero")


def test_task_count_is_idempotent_per_request():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    first = service.star_up("hero", ["mat"], task_id="daily_star", request_id="req-1")
    second = service.star_up("hero", [], task_id="daily_star", request_id="req-1")
    assert second == first
    assert service.task_count("daily_star") == 1
    assert service.card_state("hero")["star"] == 2


def test_revert_restores_star_skill_and_material():
    service = build()
    service.add_card("mat", "tpl_hero", "purple")
    service.star_up("hero", ["mat"])
    service.revert("hero")
    state = service.card_state("hero")
    assert state["star"] == 1
    assert state["skill_points"] == 9
    assert service.card_state("mat")["in_bag"] is True


def test_transfer_carries_star_and_version():
    service = cs.CardStarUp()
    service.add_card("hero", "tpl_hero", "purple", star=3)
    first = service.transfer("hero", "s2")
    second = service.transfer("hero", "s3")
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["star"] == 3
    assert service.card_state("hero")["server"] == "s3"


def test_concurrent_star_up_consumes_once():
    service = build()
    service.add_card("mat_a", "tpl_hero", "purple")
    service.add_card("mat_b", "tpl_hero", "purple")
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(material):
        barrier.wait()
        try:
            service.star_up("hero", [material])
            outcomes.append("ok")
        except Exception:
            outcomes.append("reject")

    threads = [threading.Thread(target=worker, args=(item,)) for item in ("mat_a", "mat_b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert service.card_state("hero")["star"] == 2
    consumed = [item for item in ("mat_a", "mat_b") if not service.card_state(item)["in_bag"]]
    assert len(consumed) == 1
