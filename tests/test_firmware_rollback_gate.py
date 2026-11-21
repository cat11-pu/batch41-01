import pytest

import firmware_rollback_gate as frg


def _sig(key="k1", chain=None, valid=True, not_after=None, signer="vendor"):
    return {
        "signer": signer,
        "key_id": key,
        "chain": list(chain or [key, "root1"]),
        "digest": "sha256",
        "valid": valid,
        "not_after": not_after,
    }


def _pkg(
    version=11,
    model="cam-a",
    hardware="rev2",
    signatures=None,
    incremental=False,
    baseline=None,
):
    return {
        "version": version,
        "model": model,
        "hardware": hardware,
        "signatures": [_sig()] if signatures is None else list(signatures),
        "incremental": incremental,
        "baseline": baseline,
        "payload": "firmware-bytes",
    }


def _device(**overrides):
    device = {
        "current_version": 10,
        "counter": 10,
        "secure_counter": 10,
        "partition_counters": {"A": 10, "B": 10},
        "active_partition": "A",
        "clock_synced": True,
        "installed_versions": [9, 10],
        "model": "cam-a",
        "hardware": "rev2",
    }
    device.update(overrides)
    return device


def _gate(dual_signers=()):
    return frg.FirmwareRollbackGate(
        trusted_roots={"root1"},
        trusted_intermediates={"i1"},
        revoked_keys={"k-old"},
        dual_signers=dual_signers,
    )


def test_old_signed_firmware_is_rejected_as_rollback():
    result = _gate().verify(_pkg(version=5), _device())
    assert result["allowed"] is False
    assert result["code"] == "rollback"


def test_zeroed_rollback_counter_is_detected():
    result = _gate().verify(_pkg(version=11), _device(counter=0, secure_counter=10))
    assert result["allowed"] is False
    assert result["code"] == "counter_reset"


def test_model_mismatch_is_rejected():
    result = _gate().verify(_pkg(version=11, model="cam-b"), _device())
    assert result["allowed"] is False
    assert result["code"] == "model_mismatch"


def test_incremental_package_requires_and_matches_baseline():
    missing = _gate().verify(_pkg(version=11, incremental=True, baseline=None), _device())
    assert missing["allowed"] is False
    assert missing["code"] == "missing_baseline"
    wrong = _gate().verify(_pkg(version=11, incremental=True, baseline=9), _device())
    assert wrong["allowed"] is False
    assert wrong["code"] == "baseline_mismatch"


def test_untrusted_intermediate_certificate_breaks_the_chain():
    signature = _sig(chain=["k1", "evil-ca", "root1"])
    result = _gate().verify(_pkg(version=11, signatures=[signature]), _device())
    assert result["allowed"] is False
    assert result["code"] == "untrusted_chain"


def test_partition_counters_must_be_in_sync():
    result = _gate().verify(
        _pkg(version=11), _device(partition_counters={"A": 10, "B": 7})
    )
    assert result["allowed"] is False
    assert result["code"] == "partition_desync"


def test_unsynced_clock_fails_closed_instead_of_skipping_expiry():
    signature = _sig(not_after=100)
    result = _gate().verify(
        _pkg(version=11, signatures=[signature]),
        _device(clock_synced=False),
        now=200,
    )
    assert result["allowed"] is False
    assert result["code"] == "clock_untrusted"


def test_revoked_signing_key_is_rejected():
    gate = frg.FirmwareRollbackGate(
        trusted_roots={"root1"}, revoked_keys={"k1"}
    )
    result = gate.verify(_pkg(version=11), _device())
    assert result["allowed"] is False
    assert result["code"] == "revoked_key"


def test_recovery_mode_still_requires_a_signature():
    result = _gate().verify(_pkg(version=11, signatures=[]), _device(), recovery=True)
    assert result["allowed"] is False
    assert result["code"] == "missing_signature"


def test_dual_signature_policy_rejects_a_single_signer():
    gate = _gate(dual_signers={"vendor", "customer"})
    result = gate.verify(_pkg(version=11, signatures=[_sig(signer="vendor")]), _device())
    assert result["allowed"] is False
    assert result["code"] == "dual_sign_required"


def test_empty_package_is_invalid():
    result = _gate().verify({}, _device())
    assert result["allowed"] is False
    assert result["code"] == "invalid_package"


def test_reinstalling_the_same_version_is_rejected():
    result = _gate().verify(_pkg(version=10), _device())
    assert result["allowed"] is False
    assert result["code"] == "duplicate_version"


def test_broken_signature_chain_missing_root_is_rejected():
    signature = _sig(chain=["k1"])
    result = _gate().verify(_pkg(version=11, signatures=[signature]), _device())
    assert result["allowed"] is False
    assert result["code"] == "chain_incomplete"


def test_hardware_revision_mismatch_is_rejected():
    result = _gate().verify(_pkg(version=11, hardware="rev1"), _device())
    assert result["allowed"] is False
    assert result["code"] == "hardware_mismatch"


def test_verification_state_is_isolated_per_device_and_failures_do_not_mutate():
    gate = _gate()
    device_a = _device()
    device_b = _device()
    accepted = gate.verify(_pkg(version=11), device_a)
    assert accepted["allowed"] is True
    assert accepted["code"] == "accepted"
    assert device_a["current_version"] == 11
    assert device_a["counter"] == 11
    assert device_a["secure_counter"] == 11
    assert device_a["partition_counters"] == {"A": 11, "B": 11}
    assert device_b["current_version"] == 10
    rejected = gate.verify(_pkg(version=5), device_a)
    assert rejected["allowed"] is False
    assert device_a["current_version"] == 11
