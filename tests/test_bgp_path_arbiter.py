import pytest

import bgp_path_arbiter as bgp


LOCAL_AS = 65000
PREFIX = "10.0.0.0/24"


def arbiter(**overrides):
    options = {"local_as": LOCAL_AS, "router_id": "192.0.2.1",
               "max_prefixes": 100}
    options.update(overrides)
    return bgp.BestPathArbiter(**options)


def update(instance, peer, prefix=PREFIX, as_path=(), **overrides):
    message = {"peer": peer, "prefix": prefix, "as_path": list(as_path)}
    message.update(overrides)
    return instance.update(message)


def test_own_as_in_as_path_is_rejected():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    result = update(node, "p1", as_path=[65100, LOCAL_AS, 65200])
    assert result["accepted"] is False
    assert result["reason"] == "as loop"
    assert node.fib() == {}


def test_own_as_in_confederation_segment_is_rejected():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    result = update(node, "p1", as_path=[65100],
                    confed_path=[65001, LOCAL_AS])
    assert result["accepted"] is False
    assert result["reason"] == "as loop"
    assert node.best_path(PREFIX) is None


def test_as_set_counts_as_one_in_path_length():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    node.add_peer("p2", 65200, address="203.0.113.2")
    update(node, "p1", as_path=[65101, 65102, 65103],
           as_set=[65104, 65105, 65106])
    update(node, "p2", as_path=[65201, 65202],
           as_set=[65203, 65204])
    best = node.best_path(PREFIX)
    assert best["peer"] == "p2"
    assert best["length"] == 3


def test_confederation_segment_does_not_count_towards_length():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    node.add_peer("p2", 65200, address="203.0.113.2")
    update(node, "p1", as_path=[65101],
           confed_path=[65102, 65103, 65104])
    update(node, "p2", as_path=[65201, 65202])
    best = node.best_path(PREFIX)
    assert best["peer"] == "p1"
    assert best["length"] == 1


def test_local_pref_applies_to_ebgp_learned_paths():
    node = arbiter()
    node.add_peer("ebgp-peer", 65100, address="203.0.113.1")
    node.add_peer("ibgp-peer", LOCAL_AS, address="203.0.113.2")
    update(node, "ebgp-peer", as_path=[65100, 65101, 65102],
           ebgp=True, local_pref=200)
    update(node, "ibgp-peer", as_path=[], local_pref=100)
    best = node.best_path(PREFIX)
    assert best["peer"] == "ebgp-peer"
    assert best["local_pref"] == 200


def test_med_is_ignored_between_different_neighbor_as():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    node.add_peer("p2", 65200, address="203.0.113.2")
    update(node, "p1", as_path=[65100], med=50)
    update(node, "p2", as_path=[65200], med=10)
    best = node.best_path(PREFIX)
    assert best["peer"] == "p1"
    assert best["med"] == 50


def test_med_is_compared_within_same_neighbor_as():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    node.add_peer("p2", 65100, address="203.0.113.2")
    update(node, "p1", as_path=[65100], med=50)
    update(node, "p2", as_path=[65100], med=10)
    best = node.best_path(PREFIX)
    assert best["peer"] == "p2"
    assert best["med"] == 10


def test_ebgp_next_hop_is_rewritten_to_peer_address():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.7")
    update(node, "p1", as_path=[65100], ebgp=True,
           next_hop="192.0.2.99")
    best = node.best_path(PREFIX)
    assert best["next_hop"] == "203.0.113.7"
    assert best["next_hop"] != "192.0.2.99"


def test_implicit_withdraw_replaces_old_attributes():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    update(node, "p1", as_path=[65100, 65101], local_pref=200, med=50)
    update(node, "p1", as_path=[65100])
    best = node.best_path(PREFIX)
    assert best["local_pref"] == 100
    assert best["med"] == 0
    assert best["as_path"] == [65100]
    assert best["length"] == 1


def test_route_refresh_withdraws_prefixes_not_readvertised():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    update(node, "p1", prefix="10.0.0.0/24", as_path=[65100])
    update(node, "p1", prefix="10.0.1.0/24", as_path=[65100])
    node.route_refresh("p1")
    result = node.finish_refresh("p1", ["10.0.0.0/24"])
    assert result["withdrawn"] == ["10.0.1.0/24"]
    assert node.best_path("10.0.0.0/24")["peer"] == "p1"
    assert node.best_path("10.0.1.0/24") is None
    assert list(node.fib()) == ["10.0.0.0/24"]


def test_graceful_restart_stale_path_expires_after_deadline():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    node.add_peer("p2", 65200, address="203.0.113.2")
    update(node, "p1", as_path=[65100], local_pref=200)
    update(node, "p2", as_path=[65200], local_pref=100)
    node.mark_stale("p1", 100.0)
    assert node.best_path(PREFIX)["peer"] == "p1"
    assert node.expire_stale(99.0) == []
    assert node.best_path(PREFIX)["peer"] == "p1"
    removed = node.expire_stale(100.0)
    assert removed == [(PREFIX, "p1")]
    assert node.best_path(PREFIX)["peer"] == "p2"


def test_max_prefix_exceeded_shuts_peer_and_withdraws_routes():
    node = arbiter(max_prefixes=2)
    node.add_peer("p1", 65100, address="203.0.113.1")
    assert update(node, "p1", prefix="10.0.1.0/24", as_path=[65100])["accepted"] is True
    assert update(node, "p1", prefix="10.0.2.0/24", as_path=[65100])["accepted"] is True
    exceeded = update(node, "p1", prefix="10.0.3.0/24", as_path=[65100])
    assert exceeded["accepted"] is False
    assert exceeded["reason"] == "max prefix exceeded"
    assert node.peer_state("p1") == "Idle"
    assert node.fib() == {}


def test_multipath_selection_is_stable_under_input_order():
    node = arbiter()
    candidates = [
        {"peer": "p-b", "next_hop": "203.0.113.2"},
        {"peer": "p-a", "next_hop": "203.0.113.1"},
    ]
    forward = node.multipath(candidates)
    backward = node.multipath(list(reversed(candidates)))
    assert forward == backward
    assert [path["peer"] for path in forward] == ["p-a", "p-b"]


def test_as0_announcement_is_rejected():
    node = arbiter()
    node.add_peer("p1", 65100, address="203.0.113.1")
    in_path = update(node, "p1", as_path=[65100, 0])
    assert in_path["accepted"] is False
    assert in_path["reason"] == "as0"
    in_set = update(node, "p1", as_path=[65100], as_set=[0])
    assert in_set["accepted"] is False
    assert node.best_path(PREFIX) is None


def test_update_and_withdraw_follow_event_order():
    first = arbiter()
    first.add_peer("p1", 65100, address="203.0.113.1")
    message = {"peer": "p1", "prefix": PREFIX, "as_path": [65100]}
    first.apply([message, {"action": "withdraw", "prefix": PREFIX,
                           "peer": "p1"}])
    assert first.best_path(PREFIX) is None
    second = arbiter()
    second.add_peer("p1", 65100, address="203.0.113.1")
    second.apply([{"action": "withdraw", "prefix": PREFIX, "peer": "p1"},
                  message])
    assert second.best_path(PREFIX)["peer"] == "p1"
