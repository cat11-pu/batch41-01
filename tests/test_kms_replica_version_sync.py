import hashlib

import pytest

import kms_replica_version_sync as kms


def _ev(key="k", version=1, region="r1", op="put", material=None):
    if material is None and op == "put":
        material = "secret-%s" % version
    return {
        "key": key,
        "version": version,
        "region": region,
        "op": op,
        "material": material,
    }


def test_version_numbers_compare_numerically_not_as_strings():
    gate = kms.KmsReplicaVersionSync()
    assert gate.apply_event(_ev(version="v9", material="nine"))["ok"] is True
    assert gate.apply_event(_ev(version="v10", material="ten"))["ok"] is True
    assert gate.current_version("k") == 10
    assert gate.read("k")["material"] == "ten"


def test_lagging_replica_read_is_redirected_to_a_replica_with_the_version():
    gate = kms.KmsReplicaVersionSync()
    assert gate.apply_event(_ev(version=2, region="r1"))["ok"] is True
    assert gate.lag("r2") == 2
    result = gate.read("k", region="r2")
    assert result["ok"] is True
    assert result["version"] == 2
    assert result["source_region"] == "r1"
    assert result["redirected"] is True


def test_lower_version_is_rejected_as_a_rollback():
    gate = kms.KmsReplicaVersionSync()
    assert gate.apply_event(_ev(version=2))["ok"] is True
    result = gate.apply_event(_ev(version=1, material="old"))
    assert result["ok"] is False
    assert result["code"] == "version_rollback"
    assert gate.current_version("k") == 2


def test_out_of_order_events_converge_to_the_highest_state():
    gate = kms.KmsReplicaVersionSync()
    results = gate.sync(
        [_ev(version=3, op="disable"), _ev(version=1), _ev(version=2)]
    )
    assert all(result["ok"] for result in results)
    assert gate.current_version("k") == 3
    result = gate.read("k")
    assert result["ok"] is False
    assert result["code"] == "disabled"


def test_tombstone_survives_compaction_and_blocks_resurrection():
    gate = kms.KmsReplicaVersionSync()
    assert gate.sync([_ev(version=1), _ev(version=2)])[1]["ok"] is True
    assert gate.apply_event(_ev(version=2, op="delete"))["ok"] is True
    gate.compact()
    replay = gate.apply_event(_ev(version=2, region="r2", material="revived"))
    assert replay["ok"] is False
    assert replay["code"] == "tombstoned"
    assert gate.read("k")["code"] == "deleted"


def test_concurrent_writes_resolve_to_one_deterministic_fork():
    gate = kms.KmsReplicaVersionSync()
    assert gate.apply_event(_ev(version=1, region="r2", material="bbb"))["ok"] is True
    result = gate.apply_event(_ev(version=1, region="r1", material="aaa"))
    assert result["ok"] is True
    assert result["code"] == "conflict_resolved"
    current = gate.read("k")
    assert current["material"] == "aaa"
    assert current["source_region"] == "r1"
    assert len(gate.conflicts) == 1


def test_same_region_reusing_a_version_with_new_material_is_a_conflict():
    gate = kms.KmsReplicaVersionSync()
    assert gate.apply_event(_ev(version=1, region="r1", material="aaa"))["ok"] is True
    result = gate.apply_event(_ev(version=1, region="r1", material="bbb"))
    assert result["ok"] is False
    assert result["code"] == "version_conflict"
    assert gate.read("k")["material"] == "aaa"


def test_old_versions_are_retained_for_decryption():
    gate = kms.KmsReplicaVersionSync()
    assert gate.sync(
        [
            _ev(version=1, material="m1"),
            _ev(version=2, material="m2"),
            _ev(version=3, material="m3"),
        ]
    )
    assert gate.versions("k") == [1, 2, 3]
    old = gate.read("k", version=1)
    assert old["ok"] is True
    assert old["material"] == "m1"


def test_duplicate_delivery_is_idempotent():
    gate = kms.KmsReplicaVersionSync()
    event = _ev(version=1)
    results = gate.sync([event, dict(event)])
    assert [result["code"] for result in results] == ["put", "duplicate"]
    assert gate.versions("k") == [1]


def test_retry_of_an_old_version_does_not_create_drift():
    gate = kms.KmsReplicaVersionSync()
    gate.sync([_ev(version=1, material="m1"), _ev(version=2, material="m2")])
    result = gate.apply_event(_ev(version=1, region="r2", material="m1"))
    assert result["code"] == "duplicate"
    assert gate.current_version("k") == 2
    assert gate.versions("k") == [1, 2]


def test_audit_covers_events_from_every_region():
    gate = kms.KmsReplicaVersionSync()
    gate.sync([_ev(version=1, region="r1"), _ev(version=2, region="r2")])
    regions = {entry["region"] for entry in gate.audit}
    assert {"r1", "r2"} <= regions


def test_corrupt_material_is_rejected_before_import():
    gate = kms.KmsReplicaVersionSync()
    result = gate.import_material("k", 1, "material", "deadbeef")
    assert result["ok"] is False
    assert result["code"] == "corrupt_material"
    assert gate.current_version("k") is None


def test_empty_or_missing_version_is_invalid():
    gate = kms.KmsReplicaVersionSync()
    empty = gate.apply_event(_ev(version=""))
    missing = gate.apply_event({"key": "k", "region": "r1", "op": "put"})
    assert empty["code"] == "invalid_version"
    assert missing["code"] == "invalid_version"
    assert gate.current_version("k") is None


def test_reads_from_all_regions_return_the_same_newest_version():
    gate = kms.KmsReplicaVersionSync()
    gate.sync(
        [
            _ev(version=1, region="r1"),
            _ev(version=2, region="r2"),
            _ev(version=3, region="r3"),
        ]
    )
    first = gate.read("k", region="r1")
    second = gate.read("k", region="r2")
    third = gate.read("k", region="r3")
    assert first["version"] == second["version"] == third["version"] == 3
    assert first["material"] == second["material"] == third["material"] == "secret-3"
    assert first["source_region"] == "r3"


def test_replicas_do_not_share_history_or_vectors():
    first = kms.KmsReplicaVersionSync()
    second = kms.KmsReplicaVersionSync()
    assert first.apply_event(_ev(version=1))["ok"] is True
    assert first.current_version("k") == 1
    assert second.current_version("k") is None
    assert second.versions("k") == []
    assert second.vector["r1"] == 0
