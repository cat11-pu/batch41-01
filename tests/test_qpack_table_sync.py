import pytest

import qpack_table_sync as qts


def test_stream_reset_rolls_back_its_insertions():
    table = qts.HeaderTableSync()
    table.insert("keep", "v", stream_id=0)
    first = table.insert("temp-a", "1", stream_id=7)
    second = table.insert("temp-b", "2", stream_id=7)
    table.insert("tail", "z", stream_id=0)
    assert table.dynamic_count() == 4
    assert table.reset_stream(7) == 2
    assert table.dynamic_count() == 2
    assert table.lookup(0, relative=True) == ("tail", "z")
    assert table.lookup(1, relative=True) == ("keep", "v")
    with pytest.raises(KeyError):
        table.lookup(first)
    with pytest.raises(KeyError):
        table.lookup(second)


def test_consecutive_duplicate_insert_is_deduplicated():
    table = qts.HeaderTableSync()
    first = table.insert("x-header", "1")
    second = table.insert("x-header", "1")
    assert second == first
    assert table.dynamic_count() == 1
    assert table.size() == len("x-header") + 1 + 32
    third = table.insert("x-header", "2")
    assert third != first
    assert table.dynamic_count() == 2


def test_eviction_skips_entries_with_outstanding_references():
    table = qts.HeaderTableSync(capacity=100)
    oldest = table.insert("x", "1")
    table.reference(oldest, stream_id=3)
    middle = table.insert("y", "2")
    newest = table.insert("z", "3")
    assert table.size() <= 100
    assert table.lookup(oldest) == ("x", "1")
    assert table.lookup(newest) == ("z", "3")
    with pytest.raises(KeyError):
        table.lookup(middle)


def test_blocked_stream_count_has_a_hard_limit():
    table = qts.HeaderTableSync(max_blocked=1)
    table.begin_blocked(1)
    with pytest.raises(RuntimeError):
        table.begin_blocked(2)
    table.end_blocked(1)
    table.begin_blocked(2)
    assert table.blocked == {2}


def test_absolute_and_relative_indices_resolve_differently():
    table = qts.HeaderTableSync()
    first = table.insert("x-a", "1")
    second = table.insert("x-b", "2")
    assert first == 4
    assert second == 5
    assert table.lookup(0) == (":authority", "")
    assert table.lookup(first) == ("x-a", "1")
    assert table.lookup(0, relative=True) == ("x-b", "2")
    assert table.lookup(1, relative=True) == ("x-a", "1")


def test_huffman_decode_rejects_empty_or_truncated_input():
    table = qts.HeaderTableSync()
    with pytest.raises(ValueError):
        table.huffman_decode(b"")
    with pytest.raises(ValueError):
        table.huffman_decode(b"\xff")
    assert table.huffman_decode(table.huffman_encode("abc")) == "abc"


def test_header_list_over_limit_is_rejected_before_storage():
    table = qts.HeaderTableSync(max_header_list=64)
    headers = [("a", "1"), ("b", "2")]
    with pytest.raises(ValueError):
        table.decode_header_list(headers)
    assert table.dynamic_count() == 0


def test_new_streams_are_rejected_after_goaway():
    table = qts.HeaderTableSync()
    table.goaway(3)
    table.begin_stream(3)
    with pytest.raises(RuntimeError):
        table.begin_stream(4)
    with pytest.raises(RuntimeError):
        table.insert("late", "value")


def test_worker_switch_adopts_shared_table_state():
    worker_a = qts.HeaderTableSync(worker_id=0)
    worker_a.insert("x-a", "1")
    worker_a.insert("x-b", "2")
    worker_b = qts.HeaderTableSync(worker_id=7)
    worker_b.adopt_state(worker_a.export_state())
    assert worker_b.lookup(0, relative=True) == ("x-b", "2")
    assert worker_b.lookup(1, relative=True) == ("x-a", "1")
    assert worker_b.dynamic_count() == worker_a.dynamic_count()


def test_capacity_updates_are_applied_in_arrival_order():
    table = qts.HeaderTableSync()
    history = table.apply_capacity_updates([64, 0, 128])
    assert history == [64, 0, 128]
    assert table.capacity == 128
    assert table.capacity_history == [64, 0, 128]


def test_negative_capacity_update_is_rejected_without_reordering():
    table = qts.HeaderTableSync()
    table.apply_capacity_updates([64])
    with pytest.raises(ValueError):
        table.apply_capacity_updates([-1, 32])
    assert table.capacity == 64
    assert table.capacity_history == [64]


def test_failed_transaction_does_not_leave_half_written_slots():
    table = qts.HeaderTableSync()
    table.insert("keep", "v")
    with pytest.raises(RuntimeError):
        with table.transaction():
            table.insert("half-a", "1")
            table.insert("half-b", "2")
            raise RuntimeError("abort")
    assert table.dynamic_count() == 1
    assert table.lookup(0, relative=True) == ("keep", "v")
    assert table.size() == len("keep") + 1 + 32


def test_restart_restores_capacity_and_dynamic_entries():
    table = qts.HeaderTableSync()
    table.apply_capacity_updates([512])
    table.insert("saved-a", "1")
    table.insert("saved-b", "2")
    state = table.export_state()
    restarted = qts.HeaderTableSync()
    restarted.adopt_state(state)
    assert restarted.capacity == 512
    assert restarted.lookup(0, relative=True) == ("saved-b", "2")
    assert restarted.lookup(1, relative=True) == ("saved-a", "1")


def test_huffman_roundtrip_covers_all_defined_symbols():
    table = qts.HeaderTableSync()
    payload = "abcdabc"
    encoded = table.huffman_encode(payload)
    assert isinstance(encoded, bytes)
    assert table.huffman_decode(encoded) == payload


def test_evicted_absolute_index_is_not_resolvable():
    table = qts.HeaderTableSync(capacity=68)
    old = table.insert("aaaa", "1")
    new = table.insert("bbbb", "2")
    with pytest.raises(KeyError):
        table.lookup(old)
    assert table.lookup(new) == ("bbbb", "2")
    assert table.dynamic_count() == 1
