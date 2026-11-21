import pytest

import posture_eval_loop as pel


def signals(**over):
    base = {
        "patched_at": 0,
        "disk_encrypted": True,
        "edr_running": True,
        "network": "corp",
        "offline_seconds": 0,
    }
    base.update(over)
    return base


def policies():
    return [
        {"id": "patch", "kind": "patch_age", "weight": 30, "required": True},
        {"id": "disk_encryption", "kind": "boolean", "signal": "disk_encrypted",
         "weight": 30, "required": True},
        {"id": "edr", "kind": "boolean", "signal": "edr_running", "weight": 20,
         "required": True},
        {"id": "network", "kind": "network", "weight": 20, "required": True},
    ]


def loop(**over):
    args = {"ttl": 300, "offline_grace": 3600, "max_patch_days": 5,
            "token_risk_drop": 40}
    args.update(over)
    return pel.PostureLoop(**args)


def test_compliant_device_scores_full_and_allows_token():
    engine = loop()
    engine.report("d1", "u1", signals(), 0)
    result = engine.evaluate("d1", 0)
    assert result["score"] == 100
    assert result["compliant"] is True
    assert result["failed"] == []
    token = engine.issue_token("u1", 0)
    status = engine.token_status(token["token"])
    assert status["revoked"] is False


def test_disk_encryption_off_has_weight_and_fails():
    engine = loop()
    engine.report("d1", "u1", signals(disk_encrypted=False), 0)
    result = engine.evaluate("d1", 0)
    assert result["score"] == 70
    assert result["compliant"] is False
    assert "disk_encryption" in result["failed"]


def test_missing_required_signal_fails_closed():
    engine = loop()
    report = signals()
    del report["disk_encrypted"]
    engine.report("d1", "u1", report, 0)
    result = engine.evaluate("d1", 0)
    assert result["score"] == 0
    assert result["compliant"] is False
    assert result["missing"] == ["disk_encrypted"]
    assert "disk_encryption" in result["failed"]


def test_evaluation_timeout_fails_closed():
    engine = loop()
    engine.report("d1", "u1", signals(), 0)
    result = engine.evaluate("d1", 0, timed_out=True)
    assert result["compliant"] is False
    assert result["score"] == 0
    assert result["reason"] == "timeout"


def test_patch_age_invalidates_cached_result():
    engine = loop(max_patch_days=5)
    engine.report("d1", "u1", signals(patched_at=0), 0)
    fresh = engine.evaluate("d1", 86400)
    stale = engine.evaluate("d1", 6 * 86400)
    assert fresh["compliant"] is True
    assert stale["compliant"] is False
    assert "patch" in stale["failed"]


def test_ttl_cache_marks_cached_and_recomputes():
    engine = loop(ttl=300)
    engine.report("d1", "u1", signals(), 0)
    first = engine.evaluate("d1", 0)
    warm = engine.evaluate("d1", 100)
    cold = engine.evaluate("d1", 400)
    assert first["cached"] is False
    assert warm["cached"] is True
    assert cold["cached"] is False
    assert engine.snapshot()["evaluations"] == 2


def test_network_change_invalidates_cached_result():
    engine = loop()
    engine.report("d1", "u1", signals(network="corp"), 0)
    assert engine.evaluate("d1", 0)["compliant"] is True
    engine.report("d1", "u1", signals(network="cafe"), 10)
    result = engine.evaluate("d1", 10)
    assert result["cached"] is False
    assert result["compliant"] is False
    assert "network" in result["failed"]


def test_stale_snapshot_cannot_overwrite_newer_result():
    engine = loop()
    engine.report("d1", "u1", signals(), 0, snapshot=5)
    with pytest.raises(ValueError):
        engine.report("d1", "u1", signals(disk_encrypted=False), 1, snapshot=3)
    engine.report("d1", "u1", signals(), 2, snapshot=6)
    assert engine.evaluate("d1", 2)["snapshot"] == 6


def test_offline_grace_is_enforced():
    engine = loop(offline_grace=3600)
    engine.report("d1", "u1", signals(offline_seconds=1000), 0)
    assert engine.evaluate("d1", 0)["compliant"] is True
    engine.report("d1", "u1", signals(offline_seconds=7200), 10)
    offline = engine.evaluate("d1", 10)
    assert offline["compliant"] is False
    assert offline["reason"] == "offline"


def test_policy_upgrade_reevaluates_and_revokes_token():
    engine = loop(policies=policies())
    engine.report("d1", "u1", signals(), 0)
    assert engine.evaluate("d1", 0)["compliant"] is True
    token = engine.issue_token("u1", 0)
    upgraded = policies() + [
        {"id": "firewall", "kind": "boolean", "signal": "firewall_enabled",
         "weight": 20, "required": True}
    ]
    engine.upgrade_policy(upgraded, 0)
    assert engine.token_status(token["token"])["revoked"] is True
    result = engine.evaluate("d1", 0)
    assert result["policy_version"] == 2
    assert result["compliant"] is False
    assert "firewall" in result["failed"]


def test_risk_jump_revokes_token():
    engine = loop()
    engine.report("d1", "u1", signals(), 0)
    assert engine.evaluate("d1", 0)["score"] == 100
    token = engine.issue_token("u1", 0)
    engine.report("d1", "u1", signals(disk_encrypted=False, network="cafe"), 10)
    result = engine.evaluate("d1", 10)
    assert result["score"] == 50
    assert result["token_action"] == "revoke"
    assert engine.token_status(token["token"])["revoked"] is True


def test_multi_device_aggregate_uses_worst_device():
    engine = loop()
    engine.report("d1", "u1", signals(), 0)
    engine.report("d2", "u1", signals(disk_encrypted=False), 0)
    info = engine.aggregate("u1", 0)
    assert info["devices"] == 2
    assert info["score"] == 70
    assert info["compliant"] is False
    assert info["worst_device"] == "d2"


def test_audit_includes_device_and_failed_policies():
    engine = loop()
    engine.report("d1", "u1", signals(disk_encrypted=False), 0)
    engine.evaluate("d1", 0)
    entry = engine.audit()[-1]
    assert entry["type"] == "evaluate"
    assert entry["device_id"] == "d1"
    assert entry["snapshot"] == 1
    assert entry["policy_version"] == 1
    assert "disk_encryption" in entry["failed"]


def test_empty_report_and_unknown_device_are_rejected():
    engine = loop()
    with pytest.raises(ValueError):
        engine.report("d1", "u1", {}, 0)
    with pytest.raises(ValueError):
        engine.evaluate("ghost", 0)
    with pytest.raises(ValueError):
        engine.token_status("0" * 32)


def test_duplicate_evaluation_is_idempotent_and_snapshot_isolated():
    engine = loop()
    engine.report("d1", "u1", signals(), 0)
    first = engine.evaluate("d1", 0)
    second = engine.evaluate("d1", 0)
    assert second["cached"] is True
    assert second["snapshot"] == first["snapshot"]
    assert engine.snapshot()["evaluations"] == 1
    view = engine.snapshot()
    view["devices"] = 99
    assert engine.snapshot()["devices"] == 1
