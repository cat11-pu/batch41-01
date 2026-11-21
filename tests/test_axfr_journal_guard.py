import hashlib

import pytest

import axfr_journal_guard as axfr


ZONE = "example.internal"
SOURCE = "10.0.0.1"
TSIG_KEY = "tsig-secret"


def guard(**overrides):
    options = {
        "zone": ZONE,
        "allowed_sources": {SOURCE},
        "max_records": 100,
        "tsig_key": TSIG_KEY,
        "tsig_fudge": 300,
        "journal_retention": 3,
    }
    options.update(overrides)
    return axfr.ZoneTransferGuard(**options)


def rec(op, name, value="1.1.1.1", rtype="A"):
    return {"op": op, "name": name, "type": rtype, "value": value}


def signed(records, seq=1, serial=1, tsig_time=1000.0, axfr_flag=False,
           tsig=None):
    signature = hashlib.sha256(
        "{}:{}:{}".format(TSIG_KEY, serial, seq).encode("utf-8")
    ).hexdigest()[:16]
    message = {
        "seq": seq,
        "serial": serial,
        "records": records,
        "tsig": signature if tsig is None else tsig,
        "tsig_time": tsig_time,
    }
    if axfr_flag:
        message["axfr"] = True
    return message


def test_fallback_axfr_clears_records_deleted_on_master():
    node = guard()
    node.load_zone([
        {"name": "old", "type": "A", "value": "1.1.1.1"},
        {"name": "keep", "type": "A", "value": "2.2.2.2"},
    ], serial=10)
    result = node.apply_transfer([signed([
        rec("add", "keep", "2.2.2.2"),
        rec("add", "new", "3.3.3.3"),
    ], seq=1, serial=11, axfr_flag=True)], from_addr=SOURCE, now=1000.0)
    assert result["applied"] is True
    values = {(item["name"], item["value"]) for item in node.zone_records()}
    assert ("old", "1.1.1.1") not in values
    assert ("new", "3.3.3.3") in values
    assert node.current_serial() == 11


def test_serial_comparison_handles_32_bit_wrap():
    node = guard()
    assert node.serial_gt(1, 0xFFFFFFFE) is True
    assert node.serial_gt(0xFFFFFFFE, 1) is False
    assert node.serial_gt(7, 7) is False


def test_multi_message_transfer_applies_by_sequence_not_arrival():
    node = guard()
    node.load_zone([], serial=0)
    first = signed([rec("add", "host", "1.1.1.1")], seq=1, serial=1)
    second = signed([rec("add", "host", "2.2.2.2")], seq=2, serial=2)
    result = node.apply_transfer([second, first], from_addr=SOURCE, now=1000.0)
    assert result["applied"] is True
    records = node.zone_records()
    assert len(records) == 1
    assert records[0]["value"] == "2.2.2.2"
    assert node.current_serial() == 2


def test_unsigned_later_message_invalidates_whole_transfer():
    node = guard()
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=10)
    first = signed([rec("add", "added", "3.3.3.3")], seq=1, serial=11)
    second = signed([rec("add", "unsigned", "4.4.4.4")], seq=2, serial=12,
                    tsig="")
    result = node.apply_transfer([first, second], from_addr=SOURCE,
                                 now=1000.0)
    assert result["applied"] is False
    assert result["reason"] == "bad tsig"
    names = {item["name"] for item in node.zone_records()}
    assert names == {"keep"}
    assert node.current_serial() == 10


def test_tsig_time_window_uses_absolute_epoch_and_rejects_expired():
    node = guard()
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=10)
    inside = signed([rec("add", "fresh", "3.3.3.3")], seq=1, serial=11,
                    tsig_time=800.0)
    assert node.apply_transfer([inside], from_addr=SOURCE,
                               now=1000.0)["applied"] is True
    expired = signed([rec("add", "late", "4.4.4.4")], seq=1, serial=12,
                     tsig_time=600.0)
    result = node.apply_transfer([expired], from_addr=SOURCE, now=1000.0)
    assert result["applied"] is False
    assert result["reason"] == "tsig time out of window"
    assert node.current_serial() == 11


def test_acl_uses_exact_zone_and_source_not_prefixes():
    node = guard()
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=10)
    sibling = node.handle_request({
        "source": SOURCE, "zone": "example.internal.evil", "type": "AXFR"})
    assert sibling["ok"] is False
    assert sibling["reason"] == "zone mismatch"
    wrong_source = node.handle_request({
        "source": "10.0.0.10", "zone": ZONE, "type": "AXFR"})
    assert wrong_source["ok"] is False
    assert wrong_source["reason"] == "source not allowed"
    allowed = node.handle_request({
        "source": SOURCE, "zone": ZONE, "type": "AXFR"})
    assert allowed["ok"] is True


def test_compression_pointer_loop_raises_instead_of_hanging():
    node = guard()
    assert node.compression_name(b"\x03www\x00") == "www"
    with pytest.raises(ValueError):
        node.compression_name(b"\xc0\x00")
    with pytest.raises(ValueError):
        node.compression_name(b"\x03ab")


def test_record_count_limit_rejects_oversized_transfer():
    node = guard(max_records=2)
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=5)
    result = node.apply_transfer([signed([
        rec("add", "a", "1.1.1.1"),
        rec("add", "b", "1.1.1.2"),
        rec("add", "c", "1.1.1.3"),
    ], seq=1, serial=6)], from_addr=SOURCE, now=1000.0)
    assert result["applied"] is False
    assert result["reason"] == "too many records"
    assert node.current_serial() == 5
    assert [item["name"] for item in node.zone_records()] == ["keep"]


def test_interrupted_transfer_leaves_previous_zone_intact():
    node = guard()
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=5)
    first = signed([rec("add", "half-a", "3.3.3.3")], seq=1, serial=6,
                   tsig_time=1000.0)
    second = signed([rec("add", "half-b", "4.4.4.4")], seq=2, serial=7,
                    tsig_time=100.0)
    result = node.apply_transfer([first, second], from_addr=SOURCE,
                                 now=1000.0)
    assert result["applied"] is False
    assert result["reason"] == "tsig time out of window"
    assert [item["name"] for item in node.zone_records()] == ["keep"]
    assert node.current_serial() == 5


def test_cursor_advance_is_idempotent_for_repeated_pull():
    node = guard()
    first = node.advance_cursor("slave-1", 10)
    assert first["advanced"] is True
    assert first["cursor"] == 10
    again = node.advance_cursor("slave-1", 10)
    assert again["advanced"] is False
    assert again["cursor"] == 10
    older = node.advance_cursor("slave-1", 5)
    assert older["advanced"] is False
    assert older["cursor"] == 10


def test_two_slaves_keep_independent_journal_cursors():
    node = guard()
    assert node.advance_cursor("slave-1", 10)["advanced"] is True
    assert node.advance_cursor("slave-2", 10)["advanced"] is True
    assert node.snapshot()["cursors"] == {"slave-1": 10, "slave-2": 10}
    assert node.advance_cursor("slave-2", 10)["advanced"] is False


def test_notify_before_data_does_not_advance_zone_serial():
    node = guard()
    node.load_zone([{"name": "keep", "type": "A", "value": "2.2.2.2"}],
                   serial=10)
    notified = node.notify(99, source=SOURCE)
    assert notified["accepted"] is True
    assert node.current_serial() == 10
    assert node.refresh(10) == {"needed": False, "serial": 10}
    node.apply_transfer([signed([rec("add", "new", "3.3.3.3")], seq=1,
                                serial=11, axfr_flag=True)],
                        from_addr=SOURCE, now=1000.0)
    assert node.refresh(10)["needed"] is True


def test_journal_retention_gap_falls_back_to_full_axfr():
    node = guard(journal_retention=3)
    node.load_zone([], serial=10)
    for index, serial in enumerate((11, 12, 13, 14), start=1):
        message = signed([rec("add", "h{}".format(index), "10.0.0.{}".format(index))],
                         seq=1, serial=serial)
        assert node.apply_transfer([message], from_addr=SOURCE,
                                   now=1000.0)["applied"] is True
    response = node.handle_request({
        "source": SOURCE, "zone": ZONE, "type": "IXFR", "serial": 9})
    assert response["type"] == "AXFR"
    assert response["fallback"] is True
    assert len(response["records"]) == 4


def test_ixfr_within_journal_returns_only_newer_diffs():
    node = guard(journal_retention=3)
    node.load_zone([], serial=10)
    for index, serial in enumerate((11, 12, 13, 14), start=1):
        message = signed([rec("add", "h{}".format(index), "10.0.0.{}".format(index))],
                         seq=1, serial=serial)
        node.apply_transfer([message], from_addr=SOURCE, now=1000.0)
    response = node.handle_request({
        "source": SOURCE, "zone": ZONE, "type": "IXFR", "serial": 11})
    assert response["type"] == "IXFR"
    assert "fallback" not in response
    assert [entry["serial"] for entry in response["diffs"]] == [12, 13, 14]


def test_valid_axfr_request_returns_complete_zone():
    node = guard()
    node.load_zone([
        {"name": "a", "type": "A", "value": "1.1.1.1"},
        {"name": "b", "type": "A", "value": "2.2.2.2"},
    ], serial=7)
    response = node.handle_request({
        "source": SOURCE, "zone": ZONE, "type": "AXFR"})
    assert response["ok"] is True
    assert response["type"] == "AXFR"
    assert response["serial"] == 7
    assert len(response["records"]) == 2
