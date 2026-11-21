import pytest

import ikev2_sa_negotiator as ik


def test_cookie_challenge_is_retried():
    g = ik.SaNegotiator(require_cookie=True)
    out = g.initiate(g.handle_init)
    assert out["status"] == "ok"
    assert out["retries"] == 1
    assert out["proposal"]["encr"] == "AES_256_CBC"


def test_first_common_proposal_is_selected():
    g = ik.SaNegotiator()
    offered = [
        {"encr": "AES_512_GCM", "integ": "HMAC_SHA2_512",
         "dh": "MODP_8192", "key_length": 512},
        {"encr": "AES_128_CBC", "integ": "HMAC_SHA2_256",
         "dh": "MODP_2048", "key_length": 128},
    ]
    chosen = g.select_proposal(offered)
    assert chosen["encr"] == "AES_128_CBC"
    assert chosen["key_length"] == 128


def test_common_dh_group_is_selected():
    supported = [{"encr": "AES_256_CBC", "integ": "HMAC_SHA2_256",
                  "dh": "MODP_2048", "key_length": 256}]
    g = ik.SaNegotiator(supported=supported)
    offered = [
        {"encr": "AES_256_CBC", "integ": "HMAC_SHA2_256", "dh": "MODP_1024"},
        {"encr": "AES_256_CBC", "integ": "HMAC_SHA2_256", "dh": "MODP_2048"},
    ]
    chosen = g.select_proposal(offered)
    assert chosen["dh"] == "MODP_2048"
    assert chosen["key_length"] == 256


def test_short_nonce_is_rejected():
    g = ik.SaNegotiator()
    request = {"spi_i": 9, "nonce": b"short-nonce",
               "proposals": [dict(g.supported[0])]}
    with pytest.raises(ik.IkeError) as exc:
        g.handle_init(request)
    assert exc.value.code == "short_nonce"


def test_child_key_lookup_is_bound_to_spi():
    g = ik.SaNegotiator()
    tunnel = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    child_id = tunnel["child"]["id"]
    stored = g.child(tunnel["ike_id"], child_id)
    assert stored is not None
    assert stored["keys"] == tunnel["child"]["keys"]
    assert stored["spi"] == tunnel["child"]["spi"]
    assert g.child(tunnel["ike_id"], "missing") is None
    assert g.child(999, child_id) is None


def test_two_tunnels_do_not_share_keys():
    g = ik.SaNegotiator()
    first = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    second = g.add_tunnel("b", ["10.2.0.0/8"], ["10.3.0.0/16"])
    assert first["child"]["spi"] != second["child"]["spi"]
    assert first["child"]["keys"] != second["child"]["keys"]


def test_rekey_conflict_has_single_winner():
    g = ik.SaNegotiator()
    first = g.resolve_rekey({"nonce": b"\x01\x01", "spi": 10},
                            {"nonce": b"\x02\x02", "spi": 20})
    assert first["winner"] == "local"
    second = g.resolve_rekey({"nonce": b"\x02\x02", "spi": 10},
                             {"nonce": b"\x01\x01", "spi": 20})
    assert second["winner"] == "remote"
    tie = g.resolve_rekey({"nonce": b"\x03\x03", "spi": 10},
                          {"nonce": b"\x03\x03", "spi": 20})
    assert tie["winner"] == "local"
    assert tie["loser_action"] == "delete"


def test_traffic_selector_is_narrowed_to_intersection():
    g = ik.SaNegotiator()
    assert g.narrow_selectors(["10.0.0.0/8"], ["10.1.0.0/16"]) == ["10.1.0.0/16"]
    assert g.narrow_selectors(["10.1.2.0/24"], ["10.1.0.0/16"]) == ["10.1.2.0/24"]


def test_default_selector_is_narrowed_by_peer():
    g = ik.SaNegotiator()
    assert g.narrow_selectors(["0.0.0.0/0"], ["10.1.2.0/24"]) == ["10.1.2.0/24"]


def test_deleting_one_child_keeps_ike_sa():
    g = ik.SaNegotiator()
    tunnel = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    second = g.add_child(tunnel["ike_id"], "second",
                         ["10.2.0.0/8"], ["10.3.0.0/16"])
    before = g.sa_count()
    deleted = g.delete_child(tunnel["ike_id"], tunnel["child"]["id"])
    assert deleted["deleted"] == tunnel["child"]["id"]
    assert deleted["ike_alive"] is True
    assert g.child(tunnel["ike_id"], tunnel["child"]["id"]) is None
    assert g.child(tunnel["ike_id"], second["id"]) is not None
    assert g.sa_count() == before


def test_dpd_storm_does_not_add_sa():
    g = ik.SaNegotiator()
    tunnel = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    count = g.sa_count()
    assert g.dpd(tunnel["ike_id"], 0.0)["action"] == "probe"
    for step in range(1, 5):
        assert g.dpd(tunnel["ike_id"], step * 0.1)["action"] == "hold"
    assert g.sa_count() == count


def test_nat_t_marker_is_stripped():
    g = ik.SaNegotiator()
    payload = b"\x01\x02\x03\x04"
    packet = {"src_port": 4500, "payload": b"\x00\x00\x00\x00" + payload}
    out = g.parse_nat_t(packet)
    assert out["non_esp"] is True
    assert out["payload"] == payload
    plain = {"src_port": 500, "payload": b"\x00\x00\x00\x00" + payload}
    kept = g.parse_nat_t(plain)
    assert kept["non_esp"] is False
    assert kept["payload"] == plain["payload"]


def test_replay_window_survives_rekey():
    g = ik.SaNegotiator()
    tunnel = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    ike_id = tunnel["ike_id"]
    assert g.replay_check(ike_id, 10) is True
    assert g.replay_check(ike_id, 10) is False
    assert g.replay_check(ike_id, 11) is True
    g.rekey(ike_id)
    assert g.replay_check(ike_id, 10) is False
    assert g.replay_check(ike_id, 12) is True


def test_fragments_are_reassembled_in_order():
    g = ik.SaNegotiator()
    fragments = [
        {"index": 1, "total": 2, "data": b"WORLD"},
        {"index": 0, "total": 2, "data": b"HELLO"},
    ]
    assert g.reassemble(fragments) == b"HELLOWORLD"


def test_state_survives_restart():
    g = ik.SaNegotiator()
    tunnel = g.add_tunnel("a", ["10.0.0.0/8"], ["10.1.0.0/16"])
    assert g.replay_check(tunnel["ike_id"], 10) is True
    state = g.export_state()
    other = ik.SaNegotiator()
    other.import_state(state)
    assert other.sa_count() == 1
    assert other.child(tunnel["ike_id"], tunnel["child"]["id"]) is not None
    assert other.replay_check(tunnel["ike_id"], 10) is False
    assert other.replay_check(tunnel["ike_id"], 11) is True
