import hashlib

import pytest

import eapol_authenticator as eapol


PORT = "p1"
MAC1 = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"
PRINTER = "aa:bb:cc:dd:ee:ff"


def auth(**overrides):
    options = {"mab_db": [PRINTER]}
    options.update(overrides)
    instance = eapol.EapolAuthenticator(**options)
    instance.add_port(PORT)
    return instance


def frame(frame_type, **overrides):
    packet = {"type": frame_type, "port": PORT, "mac": MAC1}
    packet.update(overrides)
    return packet


def md5_response(challenge):
    seed = challenge + ":radius-secret"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def radius_response(code="accept", now=100.0, **overrides):
    digest = hashlib.sha256(
        ("radius-secret:" + code).encode("utf-8")).hexdigest()[:16]
    packet = {
        "port": PORT,
        "code": code,
        "message_authenticator": digest,
        "timeout": False,
        "now": now,
    }
    packet.update(overrides)
    return packet


def to_radius(instance, mac=MAC1, now=100.0):
    instance.on_eapol(frame("start", mac=mac), now)
    challenge = instance.on_eapol(
        frame("identity", mac=mac, identity="alice"), now)["challenge"]
    instance.on_eapol(
        frame("md5-response", mac=mac, response=md5_response(challenge)), now)
    return challenge


def test_eapol_start_gets_identity_request():
    instance = auth()
    result = instance.on_eapol(frame("start"), 0.0)
    assert result["action"] == "request_identity"
    assert instance.port_state(PORT) == "Authenticating"


def test_empty_identity_is_rejected():
    instance = auth()
    instance.on_eapol(frame("start"), 0.0)
    result = instance.on_eapol(frame("identity", identity="   "), 0.0)
    assert result["accepted"] is False
    assert result["reason"] == "empty identity"
    assert instance.port_state(PORT) == "Unauthorized"


def test_challenge_changes_per_session_and_old_response_is_replayed_fail():
    instance = auth()
    instance.on_eapol(frame("start"), 0.0)
    first = instance.on_eapol(
        frame("identity", identity="alice"), 0.0)["challenge"]
    first_response = md5_response(first)
    assert instance.on_eapol(
        frame("md5-response", response=first_response), 0.0)["accepted"] is True
    second = instance.on_eapol(
        frame("identity", identity="alice"), 1.0)["challenge"]
    assert second != first
    replay = instance.on_eapol(
        frame("md5-response", response=first_response), 1.0)
    assert replay["accepted"] is False
    assert replay["reason"] == "bad response"


def test_forged_radius_accept_with_bad_message_authenticator_is_rejected():
    instance = auth()
    to_radius(instance)
    result = instance.on_radius(
        radius_response(code="accept", message_authenticator="0" * 16))
    assert result["authorized"] is False
    assert result["reason"] == "bad message authenticator"
    assert instance.port_state(PORT) == "Unauthorized"


def test_valid_radius_accept_authorizes_authenticated_vlan():
    instance = auth()
    to_radius(instance)
    result = instance.on_radius(radius_response(code="accept", now=100.0))
    assert result["authorized"] is True
    assert result["vlan"] == 10
    assert instance.port_state(PORT) == "Authenticated"
    assert instance.port_vlan(PORT) == 10
    assert instance.is_authorized(PORT, MAC1) is True


def test_radius_reject_lands_port_in_guest_vlan():
    instance = auth()
    to_radius(instance)
    result = instance.on_radius(radius_response(code="reject"))
    assert result["authorized"] is False
    assert result["vlan"] == 99
    assert instance.port_state(PORT) == "Guest"
    assert instance.port_vlan(PORT) == 99
    assert instance.is_authorized(PORT, MAC1) is False


def test_radius_timeout_fails_closed():
    instance = auth()
    to_radius(instance)
    result = instance.on_radius(radius_response(timeout=True))
    assert result["authorized"] is False
    assert result["reason"] == "radius timeout"
    assert instance.port_state(PORT) == "Unauthorized"
    assert instance.is_authorized(PORT, MAC1) is False


def test_mab_normalizes_mac_separators_and_case():
    instance = auth()
    assert instance.normalize_mac("AA-BB-CC-DD-EE-FF") == PRINTER
    assert instance.normalize_mac("aabb.ccdd.eeff") == PRINTER
    result = instance.try_mab(PORT, "AA-BB-CC-DD-EE-FF")
    assert result["authorized"] is True
    assert result["method"] == "mab"
    assert instance.is_authorized(PORT, PRINTER) is True


def test_8021x_start_takes_precedence_over_mab():
    instance = auth()
    instance.on_eapol(frame("start"), 0.0)
    result = instance.try_mab(PORT, "AA-BB-CC-DD-EE-FF")
    assert result["authorized"] is False
    assert result["reason"] == "dot1x in progress"
    assert instance.port_state(PORT) == "Authenticating"


def test_eapol_logoff_deauthorizes_port():
    instance = auth()
    to_radius(instance)
    instance.on_radius(radius_response(code="accept"))
    assert instance.is_authorized(PORT, MAC1) is True
    result = instance.on_eapol(frame("logoff"), 101.0)
    assert result["action"] == "deauthorize"
    assert instance.port_state(PORT) == "Unauthorized"
    assert instance.port_vlan(PORT) is None
    assert instance.is_authorized(PORT, MAC1) is False


def test_second_mac_does_not_inherit_first_authorization():
    instance = auth()
    to_radius(instance, mac=MAC1)
    instance.on_radius(radius_response(code="accept"))
    assert instance.is_authorized(PORT, MAC1) is True
    instance.on_eapol(frame("start", mac=MAC2), 101.0)
    instance.on_eapol(frame("identity", mac=MAC2, identity="bob"), 101.0)
    assert instance.is_authorized(PORT, MAC2) is False
    assert instance.is_authorized(PORT, MAC1) is True


def test_md5_success_alone_does_not_jump_to_authenticated():
    instance = auth()
    to_radius(instance)
    assert instance.port_state(PORT) == "Authenticating"
    assert instance.port_vlan(PORT) is None
    assert instance.is_authorized(PORT, MAC1) is False


def test_reauth_timer_moves_session_back_to_authenticating():
    instance = auth()
    to_radius(instance, now=100.0)
    instance.on_radius(radius_response(code="accept", now=100.0))
    assert instance.tick(3699.0) == []
    assert instance.tick(3700.0) == [PORT]
    assert instance.port_state(PORT) == "Authenticating"


def test_mab_runs_when_dot1x_is_disabled():
    instance = auth(dot1x_enabled=False)
    start = instance.on_eapol(frame("start"), 0.0)
    assert start["action"] == "ignore"
    result = instance.try_mab(PORT, "AA-BB-CC-DD-EE-FF")
    assert result["authorized"] is True
    assert instance.port_state(PORT) == "Authenticated"


def test_wrong_md5_response_is_rejected():
    instance = auth()
    instance.on_eapol(frame("start"), 0.0)
    instance.on_eapol(frame("identity", identity="alice"), 0.0)
    result = instance.on_eapol(
        frame("md5-response", response="deadbeef"), 0.0)
    assert result["accepted"] is False
    assert result["reason"] == "bad response"
    assert instance.port_state(PORT) == "Unauthorized"
