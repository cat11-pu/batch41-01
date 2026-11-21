import pytest

import seccomp_notify_broker as snb


def broker_with_process(**kwargs):
    broker = snb.SeccompNotifyBroker(**kwargs)
    broker.register_process(100, 1, tgid=100)
    return broker


def test_notification_id_reuse_is_rejected():
    broker = broker_with_process()
    broker.notify(7, 100, 1, "openat", path="/etc/passwd")
    broker.exit_process(100, 1)
    broker.register_process(200, 2)
    with pytest.raises(snb.NotifyError) as caught:
        broker.notify(7, 200, 2, "openat", path="/tmp/x")
    assert caught.value.reason == "notify_id_reused"


def test_timeout_is_not_reported_as_success():
    broker = broker_with_process(timeout_ms=500)
    broker.notify(1, 100, 1, "openat", now_ms=0)
    assert broker.tick(500) == []
    assert broker.tick(501) == [1]
    assert broker.status(1) == "timed_out"
    with pytest.raises(snb.NotifyError) as caught:
        broker.respond(1, 100, 1, allow=True, now_ms=502)
    assert caught.value.reason == "notification_timeout"


def test_fd_injection_checks_target_pid():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat")
    with pytest.raises(snb.NotifyError) as caught:
        broker.inject_fd(1, 101, 1, 9)
    assert caught.value.reason == "target_mismatch"


def test_all_threads_in_group_are_covered():
    broker = snb.SeccompNotifyBroker()
    broker.register_process(100, 1, tgid=100)
    broker.register_process(101, 1, tgid=100)
    broker.notify(1, 100, 1, "openat")
    broker.notify(2, 101, 1, "openat2")
    assert broker.pending_for_group(100) == [1, 2]
    assert broker.pending() == [1, 2]


def test_responses_are_matched_by_id_not_order():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat")
    broker.notify(2, 100, 1, "openat")
    broker.respond(2, 100, 1)
    assert broker.status(2) == "responded"
    assert broker.status(1) == "pending"
    broker.respond(1, 100, 1)
    assert broker.status(1) == "responded"


def test_path_swap_between_check_and_use_is_detected():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat", path="/etc/shadow")
    values = iter([11, 12])
    with pytest.raises(snb.NotifyError) as caught:
        broker.resolve_path(1, "/etc/shadow", lambda path: next(values))
    assert caught.value.reason == "path_changed"
    stable = lambda path: 11
    assert broker.resolve_path(1, "/etc/shadow", stable) == "/etc/shadow"


def test_openat2_and_io_uring_are_covered():
    broker = broker_with_process()
    assert broker.is_covered("openat2") is True
    assert broker.is_covered("io_uring_enter") is True
    assert broker.is_covered("read") is False


def test_restart_recovers_pending_notifications():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat")
    recovered = broker.restart()
    assert recovered["recovered"] == [1]
    assert broker.status(1) == "pending"
    assert broker.respond(1, 100, 1)["responded"] is True


def test_child_inherits_filter_tracking():
    broker = broker_with_process()
    broker.add_child(100, 200, 5)
    assert broker.is_tracked(200) is True
    assert 200 in broker.tracked_pids()
    broker.notify(1, 200, 5, "openat")
    assert broker.status(1) == "pending"


def test_action_priority_is_fixed():
    broker = broker_with_process()
    assert broker.choose_action(["notify", "trace"]) == "trace"
    assert broker.choose_action(["allow", "kill"]) == "kill"


def test_empty_syscall_is_rejected():
    broker = broker_with_process()
    with pytest.raises(ValueError, match="empty_syscall"):
        broker.notify(1, 100, 1, "")


def test_duplicate_response_is_rejected():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat")
    broker.respond(1, 100, 1)
    with pytest.raises(snb.NotifyError) as caught:
        broker.respond(1, 100, 1)
    assert caught.value.reason == "already_responded"


def test_pid_wraparound_invalidates_old_notification():
    broker = broker_with_process()
    broker.notify(8, 100, 1, "openat")
    broker.exit_process(100, 1)
    broker.register_process(100, 2)
    with pytest.raises(snb.NotifyError) as caught:
        broker.respond(8, 100, 1)
    assert caught.value.reason == "stale_notification"


def test_insufficient_fd_rights_are_rejected():
    broker = broker_with_process()
    broker.notify(1, 100, 1, "openat")
    with pytest.raises(snb.NotifyError) as caught:
        broker.inject_fd(1, 100, 1, 5, rights=("write",))
    assert caught.value.reason == "insufficient_rights"
    assert broker.inject_fd(1, 100, 1, 5, rights=("read",))["injected"] is True


def test_brokers_are_isolated():
    first = broker_with_process()
    second = snb.SeccompNotifyBroker()
    first.notify(1, 100, 1, "openat")
    assert second.pending() == []
    with pytest.raises(KeyError):
        second.status(1)
