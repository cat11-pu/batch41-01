import pytest

import ztna_connector_route as zcr


def router_with(**connectors):
    router = zcr.ZtnaConnectorRouter()
    for connector_id, values in connectors.items():
        router.add_connector(connector_id, **values)
    return router


def test_unhealthy_connector_is_not_a_candidate():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10},
        c2={"region": "eu", "latency_ms": 20},
    )
    router.set_health("c1", False)
    decision = router.route("alice", "eu")
    assert decision["connector"] == "c2"


def test_connector_behind_policy_version_is_skipped():
    router = router_with(
        c1={"region": "eu", "latency_ms": 100},
        c2={"region": "eu", "latency_ms": 10},
    )
    assert router.set_policy(1, {"c1": [], "c2": []}) is True
    assert router.sync_policy(1, ["c1"]) == ["c1"]
    decision = router.route("alice", "eu")
    assert decision["connector"] == "c1"


def test_existing_session_does_not_drift():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10},
        c2={"region": "eu", "latency_ms": 20},
    )
    first = router.route("alice", "eu", session_id="s1")
    assert first["connector"] == "c1"
    router.set_latency("c1", 200)
    second = router.route("alice", "eu", session_id="s1")
    assert second["connector"] == "c1"


def test_route_scores_region_first_then_latency():
    router = router_with(
        c1={"region": "eu", "latency_ms": 90},
        c2={"region": "eu", "latency_ms": 20},
        c3={"region": "us", "latency_ms": 5},
    )
    assert router.route("alice", "eu")["connector"] == "c2"
    assert router.route("bob", "apac")["connector"] == "c3"


def test_connector_down_migrates_sessions():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10},
        c2={"region": "eu", "latency_ms": 50},
    )
    router.open_session("s1", "alice", "c1")
    router.open_session("s2", "bob", "c1")
    report = router.connector_down("c1")
    assert report["migrated"] == {"s1": "c2", "s2": "c2"}
    assert report["failed"] == []
    assert router.session_connector("s1") == "c2"


def test_user_sessions_stay_on_one_connector():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10},
        c2={"region": "us", "latency_ms": 20},
    )
    router.open_session("s1", "alice", "c1")
    assert router.route("alice", "us")["connector"] == "c1"
    route = router.route("alice", "us", session_id="s2")
    assert route["connector"] == "c1"
    assert router.session_connector("s2") == "c1"


def test_policy_sync_loads_the_complete_rule_list():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10},
        c2={"region": "eu", "latency_ms": 20},
    )
    rules = [{"user": "alice", "action": "deny"}, {"user": "bob", "action": "allow"}]
    router.set_policy(2, {"c1": rules, "c2": [{"user": "carol", "action": "allow"}]})
    router.sync_policy(2, ["c1", "c2"])
    assert router.connector_policy("c1") == {"version": 2, "rules": rules}
    router.restart_connector("c1")
    assert router.connector_policy("c1") == {"version": 0, "rules": []}
    router.sync_policy(2, ["c1"])
    assert router.connector_policy("c1")["rules"] == rules


def test_expired_connector_certificate_is_excluded():
    router = router_with(
        c1={"region": "eu", "latency_ms": 10, "cert_expires_at": 50},
        c2={"region": "eu", "latency_ms": 20, "cert_expires_at": 1000},
    )
    assert router.route("alice", "eu", now=100)["connector"] == "c2"


def test_session_table_cap_is_enforced():
    router = zcr.ZtnaConnectorRouter(session_cap=2)
    router.add_connector("c1", "eu", 10)
    router.open_session("s1", "alice", "c1")
    router.open_session("s2", "bob", "c1")
    with pytest.raises(zcr.ConnectorError) as caught:
        router.open_session("s3", "carol", "c1")
    assert caught.value.reason == "session_cap"


def test_audit_records_connector_and_policy_version():
    router = router_with(c1={"region": "eu", "latency_ms": 10})
    router.route("alice", "eu", session_id="s1")
    row = router.audit()[-1]
    assert row["connector"] == "c1"
    assert row["policy_version"] == 0
    assert row["action"] == "route"


def test_no_healthy_candidate_raises():
    router = zcr.ZtnaConnectorRouter()
    with pytest.raises(zcr.ConnectorError) as caught:
        router.route("alice", "eu")
    assert caught.value.reason == "no_connector"


def test_conflicting_policy_is_rejected():
    router = router_with(c1={"region": "eu", "latency_ms": 10})
    with pytest.raises(zcr.ConnectorError) as caught:
        router.set_policy(
            1,
            {"c1": [
                {"user": "alice", "action": "deny"},
                {"user": "alice", "action": "allow"},
            ]},
        )
    assert caught.value.reason == "policy_conflict"
    assert router.policy_version() == 0


def test_migration_failure_is_reported():
    router = router_with(c1={"region": "eu", "latency_ms": 10})
    router.open_session("s1", "alice", "c1")
    report = router.connector_down("c1")
    assert report["migrated"] == {}
    assert report["failed"] == ["s1"]
    with pytest.raises(zcr.ConnectorError) as caught:
        router.route("alice", "eu", session_id="s1")
    assert caught.value.reason == "no_connector"


def test_duplicate_session_id_is_idempotent():
    router = zcr.ZtnaConnectorRouter(session_cap=1)
    router.add_connector("c1", "eu", 10)
    first = router.open_session("s1", "alice", "c1")
    second = router.open_session("s1", "alice", "c1")
    assert first["connector"] == second["connector"] == "c1"
    assert router.session_count() == 1


def test_routers_are_isolated():
    first = router_with(c1={"region": "eu", "latency_ms": 10})
    second = zcr.ZtnaConnectorRouter()
    decision = first.route("alice", "eu", session_id="s1")
    decision["connector"] = "tampered"
    assert first.session_connector("s1") == "c1"
    with pytest.raises(zcr.ConnectorError) as caught:
        second.route("alice", "eu")
    assert caught.value.reason == "no_connector"
    assert second.audit() == []
