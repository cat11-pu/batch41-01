import pytest

import kmod_signature_gate as ksg


def _mod(
    name="foo",
    version="1.0",
    vermagic="5.15.0-1",
    key="k1",
    issuer="root1",
    chain=None,
    digest="sha256",
    valid=True,
    params=None,
    deps=None,
    builtin=False,
):
    signature = None
    if key is not None:
        signature = {
            "key_id": key,
            "issuer": issuer,
            "chain": list(chain or [key, issuer]),
            "digest": digest,
            "valid": valid,
        }
    return {
        "name": name,
        "version": version,
        "vermagic": vermagic,
        "signature": signature,
        "params": dict(params or {}),
        "deps": list(deps or []),
        "builtin": builtin,
    }


def _gate(**overrides):
    return ksg.ModuleSignatureGate(
        trusted_keys={"k1", "k2"},
        trusted_roots={"root1"},
        trusted_intermediates={"i1"},
        revoked_keys={"k-old"},
        param_whitelist={"foo": {"debug"}},
        **overrides
    )


def test_sha1_signed_module_is_rejected_by_digest_whitelist():
    result = _gate().load(_mod(digest="sha1"))
    assert result["ok"] is False
    assert result["code"] == "weak_digest"


def test_signing_key_outside_the_keyring_is_rejected():
    result = _gate().load(_mod(key="k-evil", chain=["k-evil", "root1"]))
    assert result["ok"] is False
    assert result["code"] == "untrusted_key"


def test_stripped_signature_section_is_rejected():
    result = _gate().load(_mod(key=None))
    assert result["ok"] is False
    assert result["code"] == "missing_signature"


def test_version_magic_mismatch_is_rejected():
    result = _gate().load(_mod(vermagic="6.1.0-1"))
    assert result["ok"] is False
    assert result["code"] == "vermagic_mismatch"


def test_same_name_different_version_cannot_coexist():
    gate = _gate()
    assert gate.load(_mod(version="1.0"))["ok"] is True
    result = gate.load(_mod(version="2.0"))
    assert result["ok"] is False
    assert result["code"] == "name_conflict"
    assert gate.loaded("foo")["version"] == "1.0"


def test_revocation_is_checked_again_after_a_successful_load():
    gate = _gate()
    assert gate.load(_mod())["ok"] is True
    gate.revoke("k1")
    result = gate.load(_mod())
    assert result["ok"] is False
    assert result["code"] == "revoked_key"


def test_module_parameters_must_be_on_the_whitelist():
    result = _gate().load(_mod(params={"debug": 1, "evil": 1}))
    assert result["ok"] is False
    assert result["code"] == "param_not_allowed"


def test_builtin_module_is_exempt_in_enforcing_mode():
    result = _gate().load(_mod(key=None, builtin=True))
    assert result["ok"] is True
    assert result["code"] == "builtin_exempt"


def test_unsigned_dependency_blocks_the_parent_module():
    parent = _mod(name="foo", deps=["bar"])
    available = {"bar": _mod(name="bar", key=None)}
    result = _gate().load(parent, available=available)
    assert result["ok"] is False
    assert result["code"] == "dependency_unsigned"


def test_missing_intermediate_in_signature_chain_is_detected():
    signature_chain = ["k1", "root1"]
    result = _gate().load(_mod(issuer="i1", chain=signature_chain))
    assert result["ok"] is False
    assert result["code"] == "chain_broken"


def test_unload_then_reload_does_not_reuse_stale_signature_result():
    gate = _gate()
    assert gate.load(_mod(version="1.0"))["ok"] is True
    gate.unload("foo")
    result = gate.load(_mod(version="1.0", valid=False))
    assert result["ok"] is False
    assert result["code"] == "bad_signature"


def test_invalid_module_returns_an_explicit_code_and_reason():
    result = _gate().load({})
    assert result["ok"] is False
    assert result["code"] == "invalid_module"
    assert result["reason"]


def test_valid_module_loads_and_status_is_recorded():
    gate = _gate()
    result = gate.load(_mod(params={"debug": 1}))
    assert result["ok"] is True
    assert result["code"] == "loaded"
    assert gate.loaded("foo") == {"name": "foo", "version": "1.0", "key_id": "k1"}


def test_signed_dependency_chain_loads_successfully():
    parent = _mod(name="foo", deps=["bar"])
    available = {"bar": _mod(name="bar")}
    result = _gate().load(parent, available=available)
    assert result["ok"] is True
    assert result["code"] == "loaded"


def test_gate_state_is_isolated_between_instances():
    first = _gate()
    second = _gate()
    assert first.load(_mod())["ok"] is True
    assert second.loaded("foo") is None
    first.revoke("k1")
    assert second.load(_mod())["ok"] is True
