import pytest

import audit_gap_ledger as agl


class _Sink:
    def __init__(self, fail=False):
        self.fail = fail
        self.records = []
        self.gap_records = []

    def write(self, record):
        if self.fail:
            raise OSError("disk full")
        self.records.append(record["id"])
        return True

    def write_gap(self, gap):
        if self.fail:
            raise OSError("disk full")
        self.gap_records.append(dict(gap))
        return True


def _rec(seq, rid=None, tenant="t1", subject="s1", priority="access", op="login"):
    return {
        "id": rid or "r%d" % seq,
        "seq": seq,
        "tenant": tenant,
        "subject": subject,
        "priority": priority,
        "op": op,
        "data": "payload",
    }


def test_full_queue_records_a_gap_instead_of_silent_loss():
    gate = agl.AuditGapLedger(capacity=2)
    assert gate.append(_rec(1))["ok"] is True
    assert gate.append(_rec(2))["ok"] is True
    result = gate.append(_rec(3))
    assert result["ok"] is False
    assert result["code"] == "queue_full"
    assert gate.gaps() == [{"tenant": "t1", "start": 3, "end": 3}]


def test_adjacent_dropped_sequences_coalesce_into_one_interval():
    gate = agl.AuditGapLedger(capacity=1)
    gate.append(_rec(1))
    gate.append(_rec(2))
    gate.append(_rec(3))
    assert gate.gaps() == [{"tenant": "t1", "start": 2, "end": 3}]


def test_non_adjacent_drops_remain_separate_intervals():
    gate = agl.AuditGapLedger(capacity=1)
    gate.append(_rec(1))
    gate.append(_rec(2))
    gate.drain()
    gate.append(_rec(3))
    gate.append(_rec(4))
    assert gate.gaps() == [
        {"tenant": "t1", "start": 2, "end": 2},
        {"tenant": "t1", "start": 4, "end": 4},
    ]


def test_security_lane_survives_a_full_normal_lane():
    gate = agl.AuditGapLedger(capacity=1, security_capacity=1)
    assert gate.append(_rec(1))["ok"] is True
    assert gate.append(_rec(2))["ok"] is False
    secure = gate.append(_rec(3, priority="security"))
    assert secure["ok"] is True
    assert secure["acked"] is True


def test_ack_is_returned_only_after_a_durable_write():
    sink = _Sink()
    gate = agl.AuditGapLedger(capacity=5, sink=sink)
    result = gate.append(_rec(1))
    assert result["acked"] is True
    assert sink.records == ["r1"]


def test_disk_failure_spools_without_acknowledging():
    sink = _Sink(fail=True)
    gate = agl.AuditGapLedger(capacity=5, sink=sink)
    result = gate.append(_rec(1))
    assert result["ok"] is False
    assert result["code"] == "spooled"
    assert result["acked"] is False
    assert sink.records == []
    assert gate.stats()["spooled"] == 1


def test_flush_retries_spooled_records_after_disk_recovers():
    sink = _Sink(fail=True)
    gate = agl.AuditGapLedger(capacity=5, sink=sink)
    gate.append(_rec(1))
    sink.fail = False
    assert gate.flush() == 1
    assert sink.records == ["r1"]
    assert gate.stats()["spooled"] == 0


def test_tenant_quota_does_not_block_other_tenants():
    gate = agl.AuditGapLedger(capacity=10, tenant_quota=2)
    assert gate.append(_rec(1, tenant="t1"))["ok"] is True
    assert gate.append(_rec(2, tenant="t1"))["ok"] is True
    limited = gate.append(_rec(3, tenant="t1"))
    other = gate.append(_rec(4, tenant="t2"))
    assert limited["ok"] is False
    assert limited["code"] == "quota_exceeded"
    assert other["ok"] is True


def test_consumer_deduplicates_retried_record_ids():
    gate = agl.AuditGapLedger(capacity=5)
    gate.append(_rec(1, rid="same"))
    gate.append(_rec(2, rid="same"))
    drained = gate.drain()
    assert [record["id"] for record in drained] == ["same"]


def test_one_subject_records_are_delivered_in_sequence_order():
    gate = agl.AuditGapLedger(capacity=10)
    gate.append(_rec(3, subject="s1", op="logout"))
    gate.append(_rec(1, subject="s1", op="login"))
    gate.append(_rec(2, subject="s1", op="refresh"))
    drained = gate.drain()
    assert [record["seq"] for record in drained] == [1, 2, 3]
    assert [record["op"] for record in drained] == ["login", "refresh", "logout"]


def test_no_sampling_all_accepted_records_are_delivered():
    gate = agl.AuditGapLedger(capacity=10)
    for seq in range(1, 7):
        assert gate.append(_rec(seq))["ok"] is True
    assert len(gate.drain()) == 6


def test_empty_ledger_has_no_records_or_gaps():
    gate = agl.AuditGapLedger()
    assert gate.drain() == []
    assert gate.gaps() == []
    assert gate.stats()["accepted"] == 0
    assert gate.stats()["gaps"] == 0


def test_gap_markers_use_a_separate_reliable_channel():
    gate = agl.AuditGapLedger(capacity=1)
    gate.append(_rec(1))
    gate.append(_rec(2))
    assert len(gate.gap_channel) == 1
    assert gate.gap_channel[0]["start"] == 2
    assert gate.gaps() == [{"tenant": "t1", "start": 2, "end": 2}]


def test_gap_ledger_is_persisted_through_the_gap_channel():
    sink = _Sink()
    gate = agl.AuditGapLedger(capacity=1, sink=sink)
    gate.append(_rec(1))
    gate.append(_rec(2))
    persisted = gate.persist_gaps()
    assert persisted == [{"tenant": "t1", "start": 2, "end": 2}]
    assert sink.gap_records == [{"tenant": "t1", "start": 2, "end": 2}]


def test_ledger_instances_do_not_share_queues_or_gaps():
    first = agl.AuditGapLedger(capacity=1)
    second = agl.AuditGapLedger(capacity=5)
    first.append(_rec(1))
    first.append(_rec(2))
    assert second.append(_rec(2))["ok"] is True
    assert first.gaps() == [{"tenant": "t1", "start": 2, "end": 2}]
    assert second.gaps() == []
    drained = second.drain()
    assert [record["seq"] for record in drained] == [2]
