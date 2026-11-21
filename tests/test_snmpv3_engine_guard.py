import hashlib

import pytest

import snmpv3_engine_guard as snmp


ENGINE = "8000000001020304"
AUTH_KEYS = {"admin": "admin-auth", "ro": "ro-auth"}


def guard(**overrides):
    options = {"engine_id": ENGINE, "max_bulk": 100}
    options.update(overrides)
    return snmp.UsmGuard(**options)


def message(user="admin", level="authPriv", payload="get-request",
            digest_len=32, auth_digest=None, priv_params="cipher",
            **overrides):
    expected = hashlib.sha256(
        (AUTH_KEYS[user] + ":" + payload).encode("utf-8")).hexdigest()
    if auth_digest is None:
        auth_digest = expected[:digest_len]
    packet = {
        "user": user,
        "level": level,
        "payload": payload,
        "auth_digest": auth_digest,
        "digest_len": digest_len,
        "priv_params": priv_params,
        "engine_id": ENGINE,
        "engine_boots": 1,
        "engine_time": 100.0,
    }
    packet.update(overrides)
    return packet


def test_discovery_rejects_untrusted_engine_but_accepts_configured_one():
    node = guard(trusted_engines={"8000000001020305"})
    rejected = node.discover({"engine_id": "8000000099999999"})
    assert rejected["accepted"] is False
    assert rejected["reason"] == "untrusted engine"
    trusted = node.discover({"engine_id": "8000000001020305"})
    assert trusted["accepted"] is True


def test_discovery_accepts_local_authoritative_engine():
    node = guard()
    result = node.discover({"engine_id": ENGINE})
    assert result["accepted"] is True
    assert result["engine_id"] == ENGINE


def test_unknown_user_and_wrong_digest_return_identical_error():
    node = guard()
    unknown = node.authenticate({
        "user": "ghost", "level": "authNoPriv", "payload": "x",
        "digest_len": 32, "auth_digest": "0" * 32,
    })
    wrong = node.authenticate(message(user="ro", level="authNoPriv",
                                       priv_params="", auth_digest="0" * 32))
    assert unknown == wrong
    assert unknown == {"error": "authenticationFailure"}


def test_auth_no_priv_still_verifies_hmac():
    node = guard()
    good = message(user="ro", level="authNoPriv", priv_params="")
    assert node.authenticate(good)["ok"] is True
    tampered = dict(good)
    tampered["auth_digest"] = "0" * 32
    assert node.authenticate(tampered)["error"] == "authenticationFailure"


def test_truncated_digest_length_is_rejected():
    node = guard()
    short = message(digest_len=8)
    result = node.authenticate(short)
    assert result["error"] == "authenticationFailure"
    acceptable = message(digest_len=12)
    assert node.authenticate(acceptable)["ok"] is True


def test_auth_priv_with_empty_privacy_parameters_is_a_decryption_error():
    node = guard()
    result = node.authenticate(message(priv_params=""))
    assert result["error"] == "decryptionError"
    assert "payload" not in result


def test_timeliness_rejects_stale_boot_counter():
    node = guard()
    stale = node.check_timeliness({"engine_boots": 0, "engine_time": 100.0},
                                  100.0)
    assert stale["ok"] is False
    assert stale["reason"] == "notInTimeWindow"
    fresh = node.check_timeliness({"engine_boots": 1, "engine_time": 100.0},
                                  100.0)
    assert fresh["ok"] is True


def test_timeliness_rejects_time_outside_window():
    node = guard()
    late = node.check_timeliness({"engine_boots": 1, "engine_time": 0.0}, 151.0)
    assert late["ok"] is False
    edge = node.check_timeliness({"engine_boots": 1, "engine_time": 0.0}, 150.0)
    assert edge["ok"] is True


def test_engine_boots_persist_across_restart():
    node = guard()
    assert node.engine_boots == 1
    assert node.advance_boot() == 2
    state = node.export_state()
    restarted = guard()
    restarted.restore_state(state)
    assert restarted.engine_boots == 2
    assert restarted.check_timeliness(
        {"engine_boots": 1, "engine_time": 0.0}, 0.0)["ok"] is False
    assert restarted.check_timeliness(
        {"engine_boots": 2, "engine_time": 0.0}, 0.0)["ok"] is True


def test_view_check_respects_oid_component_boundary():
    node = guard()
    assert node.authorize_view("ro", "1.3.6.1.2.1") is True
    assert node.authorize_view("ro", "1.3.6.1.2.1.1.1.0") is True
    assert node.authorize_view("ro", "1.3.6.1.2.10.1") is False


def test_read_only_user_cannot_access_write_view():
    node = guard()
    assert node.authorize_view("ro", "1.3.6.1.4.1.9.1.1", write=True) is False
    assert node.authorize_view("admin", "1.3.6.1.4.1.9.1.1",
                               write=True) is True


def test_get_bulk_repetitions_are_capped():
    node = guard(max_bulk=100)
    too_many = node.get_bulk("ro", "1.3.6.1.2.1.1", 101)
    assert too_many["ok"] is False
    assert too_many["reason"] == "max repetitions exceeded"
    allowed = node.get_bulk("ro", "1.3.6.1.2.1.1", 100)
    assert allowed["ok"] is True
    assert allowed["repetitions"] == 100
    assert node.get_bulk("ro", "1.3.6.1.2.1.1", -1)["ok"] is False


def test_response_must_match_request_id_and_engine_id():
    node = guard()
    good = node.match_response(7, {
        "msg_id": 7, "engine_id": ENGINE, "payload": "answer"})
    assert good["ok"] is True
    assert good["payload"] == "answer"
    wrong_engine = node.match_response(7, {
        "msg_id": 7, "engine_id": "8000000099999999", "payload": "other"})
    assert wrong_engine["ok"] is False
    wrong_id = node.match_response(7, {
        "msg_id": 8, "engine_id": ENGINE, "payload": "other"})
    assert wrong_id["ok"] is False


def test_unknown_report_oid_is_not_treated_as_normal_response():
    node = guard()
    unknown = node.handle_report({"oid": "1.3.6.1.6.3.15.1.1.99.0"})
    assert unknown["ok"] is False
    assert unknown["reason"] == "unknown report oid"
    known = node.handle_report({"oid": "1.3.6.1.6.3.15.1.1.5.0"})
    assert known["ok"] is True
    assert known["kind"] == "usmStatsWrongDigests"


def test_trap_and_inform_use_the_same_local_engine_id():
    node = guard()
    trap = node.build_notification("trap")
    inform = node.build_notification("inform")
    assert trap["engine_id"] == inform["engine_id"] == ENGINE
    assert trap["kind"] == "trap"
    assert inform["kind"] == "inform"
    with pytest.raises(ValueError):
        node.build_notification("trapv1")
