import hashlib

import pytest

import ima_measure_log as iml

INIT = "00" * 20
D1 = "11" * 20
D2 = "22" * 32
D3 = "33" * 32
D4 = "44" * 20


def _extend(prev_hex, digest_hex):
    return hashlib.sha1(bytes.fromhex(prev_hex) + bytes.fromhex(digest_hex)).hexdigest()


def test_records_are_replayed_in_log_order_not_sorted_by_digest():
    log = "10 ima-ng sha256:" + D2 + " /etc/b\n10 ima-ng sha256:" + D3 + " /etc/c\n"
    result = iml.MeasurementLogReplayer().replay(log)
    forward = _extend(_extend(INIT, D2), D3)
    reverse = _extend(_extend(INIT, D3), D2)
    assert result["valid"] is True
    assert result["pcr"][10]["expected"] == forward
    assert result["pcr"][10]["expected"] != reverse


def test_duplicate_measurements_are_both_replayed():
    log = "10 ima-ng sha256:" + D2 + " /etc/a\n10 ima-ng sha256:" + D2 + " /etc/a\n"
    result = iml.MeasurementLogReplayer().replay(log)
    assert result["count"] == 2
    assert result["pcr"][10]["expected"] == _extend(_extend(INIT, D2), D2)
    assert result["pcr"][10]["expected"] != _extend(INIT, D2)


def test_hash_algorithm_may_change_between_records():
    log = "10 ima-ng sha1:" + D1 + " /etc/a\n10 ima-ng sha256:" + D2 + " /etc/b\n"
    result = iml.MeasurementLogReplayer().replay(log)
    assert [entry["algo"] for entry in result["entries"]] == ["sha1", "sha256"]
    assert result["pcr"][10]["expected"] == _extend(_extend(INIT, D1), D2)


def test_missing_pcr_index_is_not_defaulted_to_ten():
    log = "ima-ng sha256:" + D2 + " /etc/a"
    result = iml.MeasurementLogReplayer().replay(log)
    assert result["valid"] is False
    assert "missing_pcr_index" in [error["code"] for error in result["errors"]]
    assert result["entries"] == []


def test_parser_cache_is_invalidated_when_the_same_path_changes():
    replayer = iml.MeasurementLogReplayer()
    first = replayer.replay("10 ima-ng sha256:" + D2 + " /etc/a")
    second = replayer.replay("10 ima-ng sha256:" + D3 + " /etc/a")
    assert first["pcr"][10]["expected"] == _extend(INIT, D2)
    assert second["pcr"][10]["expected"] == _extend(INIT, D3)
    assert second == iml.MeasurementLogReplayer().replay(
        "10 ima-ng sha256:" + D3 + " /etc/a"
    )


def test_truncated_tail_invalidates_the_log():
    log = "10 ima-ng sha256:" + D2 + " /etc/a\n10 ima-ng sha256:" + D3
    result = iml.MeasurementLogReplayer().replay(log)
    assert result["valid"] is False
    assert "truncated_tail" in [error["code"] for error in result["errors"]]


def test_interleaved_writers_are_ordered_by_sequence_number():
    log = (
        "3|10 ima-ng sha256:" + D3 + " /c\n"
        "1|10 ima-ng sha256:" + D2 + " /a\n"
        "2|10 ima-ng sha1:" + D4 + " /b\n"
    )
    result = iml.MeasurementLogReplayer().replay(log)
    assert [entry["seq"] for entry in result["entries"]] == [1, 2, 3]
    assert result["pcr"][10]["expected"] == _extend(_extend(_extend(INIT, D2), D4), D3)


def test_escaped_newline_in_filename_is_one_record_and_unescaped():
    log = "10 ima-ng sha256:" + D2 + " /tmp/a\\nb\n"
    result = iml.MeasurementLogReplayer().replay(log)
    assert result["count"] == 1
    assert result["entries"][0]["path"] == "/tmp/a\nb"


def test_policy_gap_for_unmeasured_required_path_is_reported():
    replayer = iml.MeasurementLogReplayer(
        required_paths=("/etc/passwd", "/tmp/payload")
    )
    result = replayer.replay("10 ima-ng sha256:" + D2 + " /etc/passwd\n")
    assert result["policy_gaps"] == ["/tmp/payload"]
    assert "policy_gap" in [error["code"] for error in result["errors"]]


def test_entry_limit_rejects_oversized_log_without_partial_replay():
    log = "\n".join(
        "10 ima-ng sha256:" + D2 + " /etc/a%d" % index for index in range(3)
    )
    result = iml.MeasurementLogReplayer(max_entries=2).replay(log)
    assert result["valid"] is False
    assert "entry_limit" in [error["code"] for error in result["errors"]]
    assert result["entries"] == []
    assert result["pcr"] == {}


def test_empty_log_is_valid_with_no_entries():
    result = iml.MeasurementLogReplayer().replay("")
    assert result["valid"] is True
    assert result["count"] == 0
    assert result["entries"] == []
    assert result["pcr"] == {}
    assert result["policy_gaps"] == []


def test_unknown_template_is_rejected():
    result = iml.MeasurementLogReplayer().replay(
        "10 ima-buf sha256:" + D2 + " /etc/a"
    )
    assert result["valid"] is False
    assert "bad_template" in [error["code"] for error in result["errors"]]


def test_pcr_index_out_of_range_is_rejected():
    result = iml.MeasurementLogReplayer().replay(
        "99 ima-ng sha256:" + D2 + " /etc/a"
    )
    assert result["valid"] is False
    assert "pcr_out_of_range" in [error["code"] for error in result["errors"]]


def test_expected_values_match_supplied_actual_pcr_values():
    log = "10 ima-ng sha256:" + D2 + " /etc/a\n11 ima-ng sha1:" + D1 + " /etc/b\n"
    expected_10 = _extend(INIT, D2)
    expected_11 = _extend(INIT, D1)
    result = iml.MeasurementLogReplayer().replay(
        log, pcr_values={10: expected_10, 11: expected_11}
    )
    assert result["valid"] is True
    assert result["pcr"][10]["actual"] == expected_10
    assert result["pcr"][11]["actual"] == expected_11


def test_replayer_instances_do_not_share_limits_or_policy():
    log = "10 ima-ng sha256:" + D2 + " /etc/a\n10 ima-ng sha256:" + D3 + " /etc/b\n"
    first = iml.MeasurementLogReplayer(max_entries=1, required_paths=("/nope",))
    second = iml.MeasurementLogReplayer(max_entries=10)
    first_result = first.replay(log)
    second_result = second.replay(log)
    assert first_result["valid"] is False
    assert first_result["policy_gaps"] == []
    assert second_result["valid"] is True
    assert second_result["policy_gaps"] == []
    assert second_result["count"] == 2
