from concurrent.futures import ThreadPoolExecutor

import pytest

import lockout_quorum as lq


def make(store=None, **kwargs):
    return lq.LockoutQuorum(store=store, **kwargs)


def test_many_instances_share_one_global_count():
    store = lq.LockoutStore()
    workers = [make(store, threshold=5) for _ in range(8)]
    result = None
    for worker in workers:
        result = worker.record_failure("alice", ip="10.0.0.1", now=0)
    assert result["locked"] is True
    assert workers[-1].is_locked("alice", now=0) is True


def test_restart_does_not_reset_counters():
    store = lq.LockoutStore()
    first = make(store, threshold=5)
    for _ in range(4):
        first.record_failure("alice", now=0)
    restarted = make(store, threshold=5)
    result = restarted.record_failure("alice", now=0)
    assert result["locked"] is True


def test_successful_login_clears_all_failures():
    gate = make(threshold=5)
    for _ in range(4):
        gate.record_failure("alice", now=0)
    gate.record_success("alice", now=1)
    for _ in range(4):
        gate.record_failure("alice", now=2)
    assert gate.is_locked("alice", now=2) is False
    assert gate.record_failure("alice", now=2)["locked"] is True


def test_lock_window_uses_server_time():
    gate = make(threshold=5, lockout=900)
    for _ in range(5):
        gate.record_failure("alice", now=0)
    assert gate.is_locked("alice", now=899) is True
    assert gate.is_locked("alice", now=901) is False


def test_account_and_ip_dimensions_are_independent():
    gate = make(threshold=5, ip_threshold=3)
    ip_result = None
    for index in range(3):
        ip_result = gate.record_failure("user-%d" % index, ip="10.0.0.9", now=0)
    assert ip_result["ip_locked"] is True
    assert ip_result["account_locked"] is False
    assert gate.is_locked("user-0", ip="10.0.0.9", now=0) is True

    account_result = None
    for index in range(3):
        account_result = gate.record_failure("bob", ip="10.1.0.%d" % index, now=0)
    assert account_result["account_locked"] is False
    for index in range(3, 5):
        account_result = gate.record_failure("bob", ip="10.1.0.%d" % index, now=0)
    assert account_result["account_locked"] is True


def test_admin_unlock_ignores_stale_backfill():
    gate = make(threshold=5)
    for _ in range(5):
        gate.record_failure("alice", now=0)
    version = gate.admin_unlock("alice", now=10)
    assert gate.is_locked("alice", now=10) is False
    backfill = gate.record_failure("alice", now=11, observed_version=version - 1)
    assert backfill["ignored"] is True
    assert gate.is_locked("alice", now=11) is False
    assert gate.failure_count("alice", now=11) == 0


def test_locked_response_uses_lock_error_code():
    gate = make(threshold=5)
    for _ in range(5):
        result = gate.record_failure("alice", now=0)
    assert result["error"] == "account_locked"
    again = gate.record_failure("alice", now=1)
    assert again["error"] == "account_locked"
    assert again["error"] != "wrong_password"


def test_concurrent_increments_are_not_lost():
    store = lq.LockoutStore()
    gate = make(store, threshold=1000, window=3600)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: gate.record_failure("race", now=0), range(160)))
    assert gate.failure_count("race", now=0) == 160


def test_password_hash_does_not_pollute_the_lock_key():
    gate = make(threshold=5)
    for _ in range(3):
        gate.record_failure("Alice", now=0, password_hash="hash-one")
    gate.record_failure("alice", now=0, password_hash="hash-two")
    result = gate.record_failure("alice", now=0, password_hash="hash-three")
    assert result["locked"] is True


def test_audit_records_one_lock_event():
    gate = make(threshold=5)
    for _ in range(20):
        gate.record_failure("alice", now=0)
    lock_rows = [row for row in gate.audit() if row["action"] == "lock"]
    assert len(lock_rows) == 1
    assert lock_rows[0]["account"] == "alice"


def test_sliding_window_expires_old_failures():
    gate = make(threshold=5, window=300)
    for offset in range(4):
        gate.record_failure("alice", now=offset)
    gate.record_failure("alice", now=400)
    assert gate.failure_count("alice", now=400) == 1
    assert gate.is_locked("alice", now=400) is False


def test_empty_account_is_rejected():
    gate = make()
    with pytest.raises(ValueError, match="empty_account"):
        gate.record_failure("", now=0)


def test_over_long_account_is_rejected():
    gate = make(max_account_len=64)
    with pytest.raises(ValueError, match="account_too_long"):
        gate.record_failure("a" * 65, now=0)


def test_cross_tenant_failure_is_rejected():
    gate = make(tenant="t1")
    with pytest.raises(ValueError, match="tenant_mismatch"):
        gate.record_failure("alice", now=0, tenant="t2")


def test_separate_stores_do_not_share_locks():
    first = make(threshold=5)
    second = make(threshold=5)
    for _ in range(5):
        first.record_failure("alice", now=0)
    assert first.is_locked("alice", now=0) is True
    assert second.is_locked("alice", now=0) is False
    assert second.stats("alice", now=0) == {"failures": 0, "locked": False}
