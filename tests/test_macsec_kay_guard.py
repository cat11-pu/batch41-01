import pytest

import macsec_kay_guard as kay


SCI = "aa:bb:cc:dd:ee:ff:0001"


def guard(**overrides):
    options = {"cak_name": "cak-prod", "sci": SCI,
               "cipher_suite": "gcm-aes-128"}
    options.update(overrides)
    return kay.KayGuard(**options)


def sak(**overrides):
    payload = {
        "key_number": 1,
        "an": 0,
        "cipher": "gcm-aes-128",
        "sci": SCI,
        "key": "key-one",
        "lifetime": 30.0,
    }
    payload.update(overrides)
    return payload


def test_cak_name_mismatch_is_rejected():
    node = guard()
    joined = node.join_ca("other-cak", SCI)
    assert joined["joined"] is False
    assert joined["reason"] == "cak name mismatch"
    packet = node.receive_mka({"body": b"\x00" * 16,
                               "cak_name": "other-cak"})
    assert packet["ok"] is False
    assert packet["reason"] == "cak name mismatch"


def test_matching_cak_joins_connectivity_association():
    node = guard()
    joined = node.join_ca("cak-prod", SCI)
    assert joined["joined"] is True
    assert joined["sci"] == SCI


def test_key_server_tie_is_broken_by_sci_and_has_one_sender():
    node = guard()
    peers = [
        {"sci": "aa:bb:cc:dd:ee:ff:0001", "priority": 0},
        {"sci": "aa:bb:cc:dd:ee:ff:0002", "priority": 0},
    ]
    result = node.elect_key_server(peers)
    assert result["key_server"] == "aa:bb:cc:dd:ee:ff:0002"
    assert result["senders"] == ["aa:bb:cc:dd:ee:ff:0002"]
    reversed_result = node.elect_key_server(list(reversed(peers)))
    assert reversed_result["key_server"] == result["key_server"]


def test_wrapped_sak_key_number_cannot_be_reused():
    node = guard()
    assert node.install_sak(sak(key_number=255, an=0))["installed"] is True
    assert node.install_sak(sak(key_number=0, an=1))["installed"] is True
    replay = node.install_sak(sak(key_number=255, an=2))
    assert replay["installed"] is False
    assert replay["reason"] == "key number already used"
    assert node.sa_state(2) == "Missing"


def test_cipher_suite_mismatch_is_rejected():
    node = guard()
    result = node.install_sak(sak(cipher="gcm-aes-256"))
    assert result["installed"] is False
    assert result["reason"] == "cipher suite mismatch"
    assert node.sa_state(0) == "Missing"


def test_sci_port_mismatch_is_rejected():
    node = guard()
    result = node.install_sak(sak(sci="aa:bb:cc:dd:ee:ff:0002"))
    assert result["installed"] is False
    assert result["reason"] == "sci mismatch"
    assert node.sa_state(0) == "Missing"


def test_short_mka_frame_is_rejected_without_parsing_past_end():
    node = guard()
    short = node.receive_mka({"body": b"\x00\x00\x00\x00"})
    assert short["ok"] is False
    assert short["reason"] == "invalid length"
    good = node.receive_mka({"body": b"\x00" * 32,
                             "cak_name": "cak-prod"})
    assert good["ok"] is True


def test_installed_sak_encrypts_and_decrypts():
    node = guard()
    assert node.install_sak(sak())["installed"] is True
    assert node.sa_state(0) == "Up"
    packet = node.encrypt(5, "hello", association=0)
    assert packet["ok"] is True
    opened = node.decrypt(packet["packet"])
    assert opened["ok"] is True
    assert opened["payload"] == "hello"


def test_bad_icv_drops_packet_without_advancing_pn():
    node = guard()
    node.install_sak(sak())
    packet = node.encrypt(7, "secret", association=0)["packet"]
    tampered = dict(packet)
    tampered["icv"] = "0" * 16
    dropped = node.decrypt(tampered)
    assert dropped["ok"] is False
    assert dropped["reason"] == "icv"
    assert node.snapshot()["associations"][0]["last_pn"] == -1
    assert node.decrypt(packet)["ok"] is True


def test_replayed_packet_number_is_rejected():
    node = guard()
    node.install_sak(sak())
    packet = node.encrypt(10, "one", association=0)["packet"]
    assert node.decrypt(packet)["ok"] is True
    replay = node.decrypt(packet)
    assert replay["ok"] is False
    assert replay["reason"] == "replay"
    newer = node.encrypt(11, "two", association=0)["packet"]
    assert node.decrypt(newer)["ok"] is True


def test_packet_number_exhaustion_requests_rekey():
    node = guard()
    node.install_sak(sak())
    almost = node.encrypt(0xFFFFFFFE, "last", association=0)
    assert almost["ok"] is True
    exhausted = node.encrypt(0xFFFFFFFF, "overflow", association=0)
    assert exhausted["ok"] is False
    assert exhausted["rekey"] is True
    assert "packet" not in exhausted


def test_sa_lifetime_expiry_disables_decryption():
    node = guard()
    node.install_sak(sak(lifetime=10.0), now=0.0)
    packet = node.encrypt(1, "payload", association=0, now=0.0)["packet"]
    assert node.decrypt(packet, now=5.0)["ok"] is True
    assert node.expire(10.0) == [0]
    assert node.sa_state(0) == "Expired"
    assert node.decrypt(packet, now=10.0)["ok"] is False


def test_rotation_keeps_old_sa_usable_during_overlap():
    node = guard()
    node.install_sak(sak(key_number=1, an=0, key="old", lifetime=10.0), now=0.0)
    rotation = node.rotate(now=1.0, lifetime=100.0)
    assert rotation["rotated"] is True
    assert rotation["an"] == 1
    old_packet = node.encrypt(1, "old-data", association=0, now=1.0)["packet"]
    new_packet = node.encrypt(1, "new-data", association=1, now=1.0)["packet"]
    assert node.decrypt(old_packet, now=2.0)["payload"] == "old-data"
    assert node.decrypt(new_packet, now=2.0)["payload"] == "new-data"


def test_rotation_retires_old_sa_after_overlap_expires():
    node = guard()
    node.install_sak(sak(key_number=1, an=0, key="old", lifetime=10.0), now=0.0)
    rotation = node.rotate(now=1.0, lifetime=100.0)
    old_packet = node.encrypt(1, "old-data", association=0, now=1.0)["packet"]
    new_packet = node.encrypt(1, "new-data",
                              association=rotation["an"], now=1.0)["packet"]
    assert node.expire(12.0) == [0]
    assert node.decrypt(old_packet, now=12.0)["ok"] is False
    assert node.decrypt(new_packet, now=12.0)["payload"] == "new-data"


def test_link_flap_downs_sa_and_requires_renegotiation():
    node = guard()
    node.install_sak(sak(key_number=1, an=0, key="old"))
    pending = node.encrypt(1, "buffered", association=0)["packet"]
    node.link_down()
    assert node.sa_state(0) == "Down"
    node.link_up(now=10.0)
    blocked = node.decrypt(pending, now=10.0)
    assert blocked["ok"] is False
    assert blocked["reason"] == "renegotiate"
    assert node.install_sak(sak(key_number=2, an=0, key="new"),
                            now=10.0)["installed"] is True
    fresh = node.encrypt(2, "fresh", association=0, now=10.0)["packet"]
    assert node.decrypt(fresh, now=10.0)["payload"] == "fresh"
