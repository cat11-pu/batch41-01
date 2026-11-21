import threading

import pytest

import gem_socketing as gs


def setup_sword(gold=1000):
    service = gs.GemSocketing(gold=gold)
    service.add_item("sword", ["red", "red"])
    service.add_gem("ruby_a", "red")
    return service


def test_socket_removes_gem_from_bag():
    service = setup_sword()
    result = service.socket("sword", 0, "ruby_a")
    assert result["index"] == 0
    assert result["gem"] == "ruby_a"
    assert service.gem_state("ruby_a")["in_bag"] is False


def test_socket_rejects_color_mismatch():
    service = setup_sword()
    service.add_gem("sapphire", "blue")
    with pytest.raises(ValueError):
        service.socket("sword", 0, "sapphire")
    assert service.gem_state("sapphire")["in_bag"] is True


def test_socket_rejects_occupied_socket():
    service = setup_sword()
    service.add_gem("ruby_b", "red")
    service.socket("sword", 0, "ruby_a")
    with pytest.raises(ValueError):
        service.socket("sword", 0, "ruby_b")
    assert service.gem_state("ruby_b")["in_bag"] is True


def test_gem_cannot_be_socketed_into_two_items():
    service = setup_sword()
    service.add_item("shield", ["red"])
    service.socket("sword", 0, "ruby_a")
    with pytest.raises(ValueError):
        service.socket("shield", 0, "ruby_a")
    assert service.item_state("shield")["sockets"][0]["gem"] is None


def test_unsocket_returns_gem_and_charges_fee():
    service = setup_sword()
    service.socket("sword", 0, "ruby_a")
    result = service.unsocket("sword", 0)
    assert result["fee"] == 50
    assert service.balance() == 950
    assert service.gem_state("ruby_a")["in_bag"] is True


def test_unsocket_without_gold_changes_nothing():
    service = setup_sword(gold=20)
    service.socket("sword", 0, "ruby_a")
    with pytest.raises(ValueError):
        service.unsocket("sword", 0)
    assert service.balance() == 20
    assert service.gem_state("ruby_a")["in_bag"] is False
    assert service.item_state("sword")["sockets"][0]["gem"] == "ruby_a"


def test_merge_three_gems_into_one():
    service = gs.GemSocketing()
    for gem_id in ("g1", "g2", "g3"):
        service.add_gem(gem_id, "red", level=1)
    result = service.merge(["g1", "g2", "g3"])
    assert result["gem"]["level"] == 2
    assert result["gem"]["color"] == "red"
    assert service.gem_state("g1")["in_bag"] is False
    assert service.gem_state(result["gem"]["id"])["level"] == 2


def test_merge_rejects_mixed_levels():
    service = gs.GemSocketing()
    service.add_gem("g1", "red", level=1)
    service.add_gem("g2", "red", level=2)
    service.add_gem("g3", "red", level=1)
    with pytest.raises(ValueError):
        service.merge(["g1", "g2", "g3"])
    assert service.gem_state("g1")["in_bag"] is True


def test_bound_state_survives_unsocket():
    service = setup_sword()
    service.add_gem("bound_ruby", "red", bound=True)
    service.socket("sword", 0, "bound_ruby")
    service.unsocket("sword", 0)
    assert service.gem_state("bound_ruby")["bound"] is True


def test_upgrade_item_keeps_sockets():
    service = setup_sword()
    service.socket("sword", 0, "ruby_a")
    service.upgrade_item("sword", 5)
    state = service.item_state("sword")
    assert state["level"] == 5
    assert state["sockets"][0]["gem"] == "ruby_a"


def test_bonus_is_capped():
    service = gs.GemSocketing()
    service.add_item("helm", ["red", "red", "red", "red"])
    for index in range(4):
        gem_id = "ruby_%d" % index
        service.add_gem(gem_id, "red", level=2)
        service.socket("helm", index, gem_id)
    assert service.preview("helm")["bonus"] == 60


def test_preview_matches_item_state():
    service = setup_sword()
    service.socket("sword", 0, "ruby_a")
    service.add_gem("ruby_b", "red")
    service.socket("sword", 1, "ruby_b")
    preview = service.preview("sword")
    state = service.item_state("sword")
    assert preview["sockets"] == state["sockets"]
    assert preview["bonus"] == state["bonus"]


def test_salvage_returns_all_gems():
    service = setup_sword()
    service.socket("sword", 0, "ruby_a")
    service.add_gem("ruby_b", "red")
    service.socket("sword", 1, "ruby_b")
    result = service.salvage("sword")
    assert sorted(result["gems"]) == ["ruby_a", "ruby_b"]
    assert service.gem_state("ruby_a")["in_bag"] is True
    assert service.gem_state("ruby_b")["in_bag"] is True
    assert [slot["gem"] for slot in service.item_state("sword")["sockets"]] == [None, None]


def test_concurrent_socket_single_winner():
    service = setup_sword()
    service.add_gem("ruby_b", "red")
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(gem_id):
        barrier.wait()
        try:
            service.socket("sword", 0, gem_id)
            outcomes.append("ok")
        except Exception:
            outcomes.append("reject")

    threads = [threading.Thread(target=worker, args=(gem,)) for gem in ("ruby_a", "ruby_b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert service.preview("sword")["bonus"] == 10


def test_sync_reports_version_and_gems():
    service = setup_sword()
    service.socket("sword", 0, "ruby_a")
    first = service.sync("sword", "s2")
    second = service.sync("sword", "s3")
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["gems"] == ["ruby_a"]
