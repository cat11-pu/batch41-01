import pytest

import match_queue_recovery as mqr


def test_tickets_survive_process_restart(tmp_path):
    path = tmp_path / "queue1.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("t1", ["alice", "bob"], priority=5, now=0.0)
    queue.enqueue("t2", ["carol"], now=1.0)
    recovered = mqr.MatchQueueRecovery(path)
    info = recovered.restore(now=2.0)
    assert info["restored"] == 2
    assert {ticket["ticket_id"] for ticket in recovered.tickets()} == {"t1", "t2"}


def test_journal_covers_the_gap_after_the_last_snapshot(tmp_path):
    path = tmp_path / "queue2.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("t1", ["alice"], now=0.0)
    queue.persist(now=0.0)
    queue.enqueue("t2", ["bob"], now=1.0)
    recovered = mqr.MatchQueueRecovery(path)
    recovered.restore(now=2.0)
    assert {ticket["ticket_id"] for ticket in recovered.tickets()} == {"t1", "t2"}


def test_cancel_writes_a_tombstone_that_survives_restart(tmp_path):
    path = tmp_path / "queue3.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("t1", ["alice"], now=0.0)
    result = queue.cancel("t1", now=1.0)
    assert result["cancelled"] is True
    recovered = mqr.MatchQueueRecovery(path)
    recovered.restore(now=2.0)
    assert recovered.ticket("t1")["status"] == "cancelled"
    assert recovered.cancel("t1", now=3.0)["cancelled"] is False


def test_cancelled_ticket_cannot_be_claimed_or_matched(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue4.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    queue.cancel("t1", now=1.0)
    assert queue.claim("t1", "node-a", now=2.0) is None
    with pytest.raises(ValueError):
        queue.match(["t1"], "m1", "node-a", now=3.0)


def test_lease_prevents_a_second_node_from_claiming(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue5.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    first = queue.claim("t1", "node-a", now=0.0, lease=10.0)
    assert first["node_id"] == "node-a"
    assert queue.claim("t1", "node-b", now=5.0, lease=10.0) is None
    second = queue.claim("t1", "node-b", now=11.0, lease=10.0)
    assert second["node_id"] == "node-b"


def test_two_nodes_cannot_match_the_same_ticket(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue6.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    queue.match(["t1"], "match-a", "node-a", now=1.0)
    with pytest.raises(ValueError):
        queue.match(["t1"], "match-b", "node-b", now=2.0)
    assert len(queue.matches()) == 1


def test_match_creation_is_idempotent_by_match_id(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue7.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    first = queue.match(["t1"], "m1", "node-a", now=1.0)
    second = queue.match(["t1"], "m1", "node-a", now=2.0)
    assert first["match_id"] == "m1"
    assert second["match_id"] == "m1"
    assert len(queue.matches()) == 1


def test_group_ticket_persists_every_member(tmp_path):
    path = tmp_path / "queue8.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("g1", ["leader", "member1", "member2"], group="g1", now=0.0)
    recovered = mqr.MatchQueueRecovery(path)
    recovered.restore(now=1.0)
    assert recovered.ticket("g1")["players"] == ["leader", "member1", "member2"]
    result = recovered.match(["g1"], "mg", "node-a", now=2.0)
    assert result["players"] == ["leader", "member1", "member2"]


def test_ticket_that_expired_while_down_is_not_requeued(tmp_path):
    path = tmp_path / "queue9.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("t1", ["alice"], expires_at=100.0, now=0.0)
    queue.persist(now=0.0)
    recovered = mqr.MatchQueueRecovery(path)
    info = recovered.restore(now=200.0)
    assert info["expired"] == ["t1"]
    assert recovered.ticket("t1")["status"] == "expired"
    assert recovered.claim("t1", "node-a", now=201.0) is None


def test_reconnect_reuses_the_live_ticket_without_duplicates(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue10.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    reconnected = queue.reconnect("alice", now=1.0)
    assert reconnected["ticket_id"] == "t1"
    assert len(queue.tickets()) == 1
    queue.cancel("t1", now=2.0)
    replacement = queue.reconnect("alice", now=3.0)
    assert replacement["ticket_id"] != "t1"
    assert queue.ticket("t1")["status"] == "cancelled"


def test_failed_match_creation_returns_tickets_to_the_queue(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue11.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    with pytest.raises(RuntimeError):
        queue.match(["t1"], "m1", "node-a", now=1.0, fail_create=True)
    assert queue.ticket("t1")["status"] == "queued"
    assert queue.matches() == []
    result = queue.match(["t1"], "m2", "node-a", now=2.0)
    assert result["match_id"] == "m2"


def test_compensation_is_idempotent(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue12.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    queue.match(["t1"], "m1", "node-a", now=1.0)
    first = queue.compensate("m1", now=2.0)
    second = queue.compensate("m1", now=3.0)
    assert first["refunded"] == ["t1"]
    assert second["already_compensated"] is True
    assert queue.ticket("t1")["status"] == "queued"
    assert len(queue.matches()) == 1


def test_priority_is_restored_and_orders_the_queue(tmp_path):
    path = tmp_path / "queue13.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("low", ["alice"], priority=1, now=0.0)
    queue.enqueue("high", ["bob"], priority=100, now=1.0)
    recovered = mqr.MatchQueueRecovery(path)
    recovered.restore(now=2.0)
    assert recovered.ticket("high")["priority"] == 100
    assert recovered.peek()["ticket_id"] == "high"


def test_stale_version_write_is_rejected(tmp_path):
    queue = mqr.MatchQueueRecovery(tmp_path / "queue14.db")
    queue.enqueue("t1", ["alice"], now=0.0)
    version = queue.ticket("t1")["version"]
    queue.update("t1", {"priority": 9}, expected_version=version, now=1.0)
    assert queue.ticket("t1")["priority"] == 9
    with pytest.raises(ValueError):
        queue.update("t1", {"priority": 5}, expected_version=version, now=2.0)
    assert queue.ticket("t1")["priority"] == 9


def test_repeated_restore_does_not_duplicate_tickets(tmp_path):
    path = tmp_path / "queue15.db"
    queue = mqr.MatchQueueRecovery(path)
    queue.enqueue("t1", ["alice"], now=0.0)
    recovered = mqr.MatchQueueRecovery(path)
    recovered.restore(now=1.0)
    recovered.restore(now=2.0)
    assert len(recovered.tickets()) == 1
    assert recovered.matches() == []
