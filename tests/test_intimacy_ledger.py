import pytest

import intimacy_ledger as il


def ledger():
    book = il.IntimacyLedger()
    book.add_friend("alice", "bob")
    return book


def test_daily_gain_is_idempotent_by_key():
    book = ledger()
    first = book.gain("alice", "bob", 40, day=1, key="dungeon-1")
    second = book.gain("alice", "bob", 40, day=1, key="dungeon-1")
    assert first["applied"] == 40
    assert second["duplicate"] is True
    assert second["applied"] == 0
    assert book.detail("alice", "bob")["intimacy"] == 40


def test_gain_clamps_at_daily_cap():
    book = ledger()
    book.gain("alice", "bob", 150, day=1, key="a")
    result = book.gain("alice", "bob", 100, day=1, key="b")
    assert result["applied"] == 50
    assert result["clamped"] is True
    assert book.detail("alice", "bob")["intimacy"] == 200


def test_gain_at_cap_raises():
    book = ledger()
    book.gain("alice", "bob", 200, day=1, key="a")
    with pytest.raises(ValueError):
        book.gain("alice", "bob", 10, day=1, key="b")
    assert book.detail("alice", "bob")["intimacy"] == 200


def test_group_bonus_uses_join_snapshot():
    book = ledger()
    token = book.join_group("alice", "bob", 2)
    book.join_group("alice", "bob", 8)
    result = book.gain("alice", "bob", 100, day=1, key="run", source="group", group_token=token)
    assert result["applied"] == 120


def test_event_and_item_bonus_are_separate():
    book = ledger()
    book.set_event_multiplier(2.0)
    result = book.gain("alice", "bob", 40, day=1, key="event", item_bonus=0.5)
    assert result["applied"] == 100


def test_claim_is_idempotent_per_tier():
    book = ledger()
    book.gain("alice", "bob", 200, day=1, key="a")
    assert book.claim("alice", "bob", 1)["claimed"] is True
    with pytest.raises(ValueError):
        book.claim("alice", "bob", 1)


def test_readd_friend_starts_from_zero():
    book = ledger()
    book.gain("alice", "bob", 120, day=1, key="a")
    book.remove_friend("alice", "bob")
    book.add_friend("alice", "bob")
    detail = book.detail("alice", "bob")
    assert detail["intimacy"] == 0
    assert detail["level"] == 0


def test_block_filters_gain():
    book = ledger()
    book.block("alice", "bob")
    with pytest.raises(PermissionError):
        book.gain("alice", "bob", 10, day=1, key="blocked")
    assert book.detail("alice", "bob")["intimacy"] == 0


def test_decay_uses_world_day_delta():
    book = ledger()
    book.gain("alice", "bob", 200, day=0, key="a")
    book.gain("alice", "bob", 200, day=1, key="b")
    book.gain("alice", "bob", 100, day=2, key="c")
    book.decay("alice", to_day=30)
    assert book.detail("alice", "bob")["intimacy"] == 350


def test_decay_same_day_is_noop():
    book = ledger()
    book.gain("alice", "bob", 200, day=0, key="a")
    book.gain("alice", "bob", 200, day=1, key="b")
    book.gain("alice", "bob", 100, day=2, key="c")
    book.decay("alice", to_day=30)
    book.decay("alice", to_day=30)
    assert book.detail("alice", "bob")["intimacy"] == 350


def test_transfer_bumps_version():
    book = ledger()
    book.gain("alice", "bob", 100, day=1, key="a")
    first = book.transfer("alice", "s2")
    second = book.transfer("alice", "s3")
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["intimacy_total"] == 100


def test_transfer_keeps_claim_history():
    book = ledger()
    book.gain("alice", "bob", 200, day=1, key="a")
    book.claim("alice", "bob", 1)
    book.transfer("alice", "s2")
    with pytest.raises(ValueError):
        book.claim("alice", "bob", 1)


def test_board_and_detail_agree():
    book = ledger()
    book.add_friend("alice", "carol")
    book.gain("alice", "bob", 120, day=1, key="b")
    book.gain("alice", "carol", 80, day=1, key="c")
    board = book.board("alice")
    assert [entry["friend"] for entry in board] == ["bob", "carol"]
    for entry in board:
        detail = book.detail("alice", entry["friend"])
        assert entry["intimacy"] == detail["intimacy"]
        assert entry["level"] == detail["level"]


def test_daily_counter_resets_on_next_day():
    book = ledger()
    book.gain("alice", "bob", 200, day=1, key="same-key")
    result = book.gain("alice", "bob", 200, day=2, key="same-key")
    assert result["duplicate"] is False
    assert book.detail("alice", "bob")["intimacy"] == 400


def test_level_thresholds():
    book = ledger()
    book.gain("alice", "bob", 99, day=1, key="a")
    assert book.detail("alice", "bob")["level"] == 0
    book.gain("alice", "bob", 1, day=1, key="b")
    assert book.detail("alice", "bob")["level"] == 1
