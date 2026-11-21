import pytest

import diameter_credit_guard as dg


def ccr(session_id="s1", subscriber="user1", number=0, kind="INITIAL",
        hop_id=1, end_id=1, result_code=2001, granted=(), used=0,
        final_unit=None, node="pcrf-a", now=0.0, avps=None):
    return {
        "session_id": session_id,
        "subscriber": subscriber,
        "request_number": number,
        "type": kind,
        "hop_id": hop_id,
        "end_id": end_id,
        "result_code": result_code,
        "granted_units": list(granted),
        "used_units": used,
        "final_unit": final_unit,
        "node": node,
        "now": now,
        "avps": list(avps or []),
    }


def test_initial_request_creates_session_and_grants():
    g = dg.CreditGuard()
    out = g.credit_control(ccr(granted=[10]))
    assert out["session_id"] == "s1"
    assert out["result_code"] == 2001
    assert out["granted_units"] == 10
    assert out["allowed"] is True
    assert g.available("user1") == 10
    assert g.active_sessions() == ["s1"]


def test_duplicate_initial_request_is_idempotent():
    g = dg.CreditGuard()
    first = g.credit_control(ccr(granted=[10]))
    second = g.credit_control(ccr(granted=[10]))
    assert first["existing"] is False
    assert second["existing"] is True
    assert second["granted_units"] == 10
    assert g.available("user1") == 10
    assert g.active_sessions() == ["s1"]


def test_concurrent_initial_for_same_subscriber_keeps_one_session():
    g = dg.CreditGuard()
    g.credit_control(ccr(session_id="s1", granted=[10]))
    second = g.credit_control(ccr(session_id="s2", granted=[10]))
    assert second["session_id"] == "s1"
    assert second["existing"] is True
    assert g.active_sessions() == ["s1"]
    assert g.available("user1") == 10


def test_duplicate_update_is_idempotent():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10]))
    g.credit_control(ccr(number=1, kind="UPDATE", granted=[5]))
    duplicate = g.credit_control(ccr(number=1, kind="UPDATE", granted=[5]))
    assert duplicate["existing"] is True
    assert g.available("user1") == 15


def test_stale_update_number_does_not_regrant():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10]))
    g.credit_control(ccr(number=1, kind="UPDATE", granted=[10]))
    g.credit_control(ccr(number=2, kind="UPDATE", granted=[5]))
    stale = g.credit_control(ccr(number=1, kind="UPDATE", granted=[10]))
    assert stale["existing"] is True
    assert stale["granted_units"] == 10
    assert g.available("user1") == 25


def test_granted_units_are_summed_once():
    g = dg.CreditGuard()
    first = g.credit_control(ccr(granted=[10, 5]))
    assert first["granted_units"] == 15
    again = g.credit_control(ccr(granted=[10, 5]))
    assert again["granted_units"] == 15
    assert g.available("user1") == 15


def test_available_credit_never_goes_negative():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10]))
    out = g.credit_control(ccr(number=1, kind="UPDATE", used=15))
    assert out["allowed"] is False
    assert g.available("user1") == 0


def test_transient_result_code_does_not_allow_service():
    g = dg.CreditGuard()
    out = g.credit_control(ccr(result_code=3004, granted=[10]))
    assert out["allowed"] is False
    assert out["granted_units"] == 0
    assert g.available("user1") == 0


def test_final_unit_redirect_blocks_service():
    g = dg.CreditGuard()
    unit = {"action": "REDIRECT", "redirect": "http://portal.example/"}
    out = g.credit_control(ccr(final_unit=unit))
    assert out["allowed"] is False
    assert out["final_action"] == "REDIRECT"
    assert out["redirect_uri"] == "http://portal.example/"
    later = g.credit_control(ccr(number=1, kind="UPDATE", granted=[10]))
    assert later["allowed"] is False
    assert later["granted_units"] == 0
    assert g.available("user1") == 0


def test_grants_from_multiple_nodes_accumulate():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10], node="pcrf-a"))
    g.credit_control(ccr(number=1, kind="UPDATE", granted=[5], node="pcrf-b"))
    assert g.available("user1") == 15


def test_response_matching_requires_both_ids():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[1], hop_id=5, end_id=6))
    assert g.match_response({"session_id": "s1", "hop_id": 5, "end_id": 7}) is None
    assert g.match_response({"session_id": "s1", "hop_id": 7, "end_id": 6}) is None
    matched = g.match_response({"session_id": "s1", "hop_id": 5, "end_id": 6})
    assert matched is not None
    assert matched["request_number"] == 0


def test_malformed_avp_is_rejected():
    g = dg.CreditGuard()
    bad_sets = [
        [{"code": 1, "vendor": 0, "length": 4, "value": b""}],
        [{"code": 2, "vendor": 1, "length": 8, "value": b""}],
    ]
    for avps in bad_sets:
        with pytest.raises(dg.CreditError) as exc:
            g.credit_control(ccr(avps=avps))
        assert exc.value.code == "malformed_avp"


def test_cleanup_releases_session_quota():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10], now=0.0))
    assert g.cleanup(61.0) == 1
    assert g.session("s1") is None
    assert g.available("user1") == 0


def test_export_and_import_preserve_sessions():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10]))
    state = g.export_state()
    other = dg.CreditGuard()
    other.import_state(state)
    assert other.active_sessions() == ["s1"]
    assert other.available("user1") == 10


def test_billing_log_records_granted_units():
    g = dg.CreditGuard()
    g.credit_control(ccr(granted=[10]))
    g.credit_control(ccr(number=1, kind="UPDATE", granted=[5], used=3))
    entries = g.log()
    assert len(entries) == 2
    assert entries[0]["granted"] == 10
    assert entries[1]["granted"] == 5
    assert entries[1]["used"] == 3
    assert entries[1]["session_id"] == "s1"
