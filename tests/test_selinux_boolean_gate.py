import pytest

import selinux_boolean_gate as sbg


def _gate(**overrides):
    return sbg.SelinuxBooleanGate(
        booleans={
            "httpd_can_network_connect": {"default": False},
            "a": {"default": False, "requires": ["b"]},
            "b": {"default": False},
        },
        **overrides
    )


def test_persistent_value_survives_reboot_and_nonpersistent_one_does_not():
    gate = _gate()
    assert gate.set_boolean("httpd_can_network_connect", True)["ok"] is True
    state = gate.state()
    assert state["runtime"]["httpd_can_network_connect"] is True
    assert state["persisted"]["httpd_can_network_connect"] is True
    gate.reboot()
    assert gate.get_boolean("httpd_can_network_connect") is True
    assert gate.set_boolean("httpd_can_network_connect", False, persist=False)["ok"] is True
    assert gate.get_boolean("httpd_can_network_connect") is False
    gate.reboot()
    assert gate.get_boolean("httpd_can_network_connect") is True


def test_boolean_name_variants_normalise_to_one_name():
    gate = _gate()
    assert gate.set_boolean("HTTPD-CAN-NETWORK-CONNECT", True)["ok"] is True
    assert gate.get_boolean("httpd_can_network_connect") is True
    assert gate.get_boolean("HTTPD-CAN-NETWORK-CONNECT") is True
    runtime = gate.state()["runtime"]
    assert runtime.get("httpd_can_network_connect") is True
    assert "HTTPD-CAN-NETWORK-CONNECT" not in runtime
    assert list(runtime).count("httpd_can_network_connect") == 1


def test_higher_priority_module_replaces_the_lower_one():
    gate = _gate()
    low = {"name": "m", "priority": 100, "version": 1, "types": ["foo_t"]}
    high = {"name": "m", "priority": 200, "version": 1, "types": ["foo_t"]}
    assert gate.load_module(low)["ok"] is True
    result = gate.load_module(high)
    assert result["ok"] is True
    assert result["code"] == "replaced"
    assert gate.modules["m"]["priority"] == 200


def test_permissive_domain_is_allowed_but_marked_unenforced():
    gate = _gate()
    gate.set_domain_permissions("httpd_t", {"network_connect"})
    gate.mark_permissive("unconfined_t")
    allowed = gate.decide("httpd_t", "network_connect")
    assert allowed["allowed"] is True
    assert allowed["enforced"] is True
    assert allowed["mode"] == "enforcing"
    permissive = gate.decide("unconfined_t", "write")
    assert permissive["allowed"] is True
    assert permissive["enforced"] is False
    assert permissive["mode"] == "permissive"
    denied = gate.decide("httpd_t", "read_shadow")
    assert denied["allowed"] is False
    assert denied["enforced"] is True


def test_relabel_reaches_every_child_path():
    gate = _gate(
        filesystem={
            "/srv": ["/srv/a", "/srv/a/b"],
            "/srv/a": ["/srv/a/b"],
            "/srv/a/b": [],
        }
    )
    result = gate.relabel("/srv", "httpd_sys_content_t")
    assert result["ok"] is True
    assert gate.contexts["/srv"] == "httpd_sys_content_t"
    assert gate.contexts["/srv/a"] == "httpd_sys_content_t"
    assert gate.contexts["/srv/a/b"] == "httpd_sys_content_t"


def test_unload_is_rejected_while_another_module_needs_the_type():
    gate = _gate()
    base = {"name": "base", "priority": 100, "version": 1, "types": ["foo_t"]}
    user = {
        "name": "user",
        "priority": 100,
        "version": 1,
        "types": ["bar_t"],
        "requires_types": ["foo_t"],
    }
    assert gate.load_module(base)["ok"] is True
    assert gate.load_module(user)["ok"] is True
    result = gate.unload_module("base")
    assert result["ok"] is False
    assert result["code"] == "type_in_use"
    assert "base" in gate.modules


def test_batch_load_is_atomic_when_one_module_is_invalid():
    gate = _gate()
    batch = [
        {"name": "ok1", "priority": 100, "version": 1, "types": ["a_t"]},
        {
            "name": "bad",
            "priority": 100,
            "version": 1,
            "types": ["b_t"],
            "requires_types": ["missing_t"],
        },
    ]
    result = gate.load_batch(batch)
    assert result["ok"] is False
    assert result["code"] == "missing_type"
    assert gate.modules == {}


def test_boolean_dependency_must_be_enabled_first():
    gate = _gate()
    blocked = gate.set_boolean("a", True)
    assert blocked["ok"] is False
    assert blocked["code"] == "dependency_missing"
    assert gate.get_boolean("a") is False
    assert gate.set_boolean("b", True)["ok"] is True
    allowed = gate.set_boolean("a", True)
    assert allowed["ok"] is True
    assert gate.get_boolean("a") is True


def test_boolean_audit_records_old_and_new_values():
    gate = _gate()
    gate.set_boolean("httpd_can_network_connect", True)
    entry = gate.audit[-1]
    assert entry["name"] == "httpd_can_network_connect"
    assert entry["old"] is False
    assert entry["new"] is True
    assert entry["persisted"] is True


def test_unknown_boolean_value_fails_closed_without_state_change():
    gate = _gate()
    result = gate.set_boolean("httpd_can_network_connect", "maybe")
    assert result["ok"] is False
    assert result["code"] == "invalid_value"
    assert gate.get_boolean("httpd_can_network_connect") is False
    assert gate.audit == []


def test_empty_policy_module_is_rejected():
    gate = _gate()
    result = gate.load_module({})
    assert result["ok"] is False
    assert result["code"] == "invalid_module"


def test_loading_the_same_module_twice_is_idempotent():
    gate = _gate()
    module = {"name": "m", "priority": 100, "version": 1, "types": ["foo_t"]}
    assert gate.load_module(module)["ok"] is True
    result = gate.load_module(dict(module))
    assert result["ok"] is True
    assert result["code"] == "already_loaded"
    assert list(gate.modules) == ["m"]


def test_lower_version_is_rejected_as_a_rollback():
    gate = _gate()
    current = {"name": "m", "priority": 100, "version": 2, "types": ["foo_t"]}
    older = {"name": "m", "priority": 100, "version": 1, "types": ["foo_t"]}
    assert gate.load_module(current)["ok"] is True
    result = gate.load_module(older)
    assert result["ok"] is False
    assert result["code"] == "version_rollback"
    assert gate.modules["m"]["version"] == 2


def test_conflicting_context_for_an_existing_path_is_rejected():
    gate = _gate(contexts={"/srv": "old_t"}, filesystem={"/srv": []})
    result = gate.relabel("/srv", "new_t")
    assert result["ok"] is False
    assert result["code"] == "context_conflict"
    assert gate.contexts["/srv"] == "old_t"


def test_gate_instances_do_not_share_booleans_or_modules():
    first = _gate()
    second = _gate()
    assert first.set_boolean("httpd_can_network_connect", True)["ok"] is True
    assert second.get_boolean("httpd_can_network_connect") is False
    module = {"name": "m", "priority": 100, "version": 1, "types": ["foo_t"]}
    assert first.load_module(module)["ok"] is True
    assert second.modules == {}
