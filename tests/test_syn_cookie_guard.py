import pytest

import syn_cookie_guard as sc


def syn(src="1.2.3.4", sport=40000, dst="9.9.9.9", dport=80, family=4,
        seq=1000, options=None):
    return {
        "flags": ["SYN"],
        "src": src,
        "sport": sport,
        "dst": dst,
        "dport": dport,
        "family": family,
        "seq": seq,
        "ack": 0,
        "options": dict(options if options is not None else
                        {"mss": 1460, "wscale": 7, "tsval": 111, "tsecr": 0}),
    }


def ack(cookie, src="1.2.3.4", sport=40000, dst="9.9.9.9", dport=80,
        family=4, seq=5000, options=None):
    return {
        "flags": ["ACK"],
        "src": src,
        "sport": sport,
        "dst": dst,
        "dport": dport,
        "family": family,
        "seq": seq,
        "ack": cookie,
        "options": dict(options or {}),
    }


def test_cookie_mode_used_when_queue_is_full():
    g = sc.SynCookieGuard(queue_limit=2, sync_threshold=99)
    assert g.receive_syn(syn(src="10.0.0.1"), 0.0)["action"] == "synack"
    assert g.receive_syn(syn(src="10.0.0.2"), 0.0)["action"] == "synack"
    out = g.receive_syn(syn(src="10.0.0.3"), 0.0)
    assert out["action"] == "cookie_synack"
    assert isinstance(out["cookie"], int)
    assert out["seq"] == out["cookie"]


def test_cookie_is_bound_to_source_port():
    g = sc.SynCookieGuard(queue_limit=0)
    first = g.receive_syn(syn(sport=40000), 0.0)
    second = g.receive_syn(syn(sport=40001), 0.0)
    assert first["cookie"] != second["cookie"]
    assert g.receive_ack(ack(first["cookie"], sport=40000), 1.0)["accepted"] is True


def test_ack_from_wrong_port_is_rejected():
    g = sc.SynCookieGuard(queue_limit=0)
    cookie = g.receive_syn(syn(sport=40000), 0.0)["cookie"]
    assert g.receive_ack(ack(cookie, sport=40001), 1.0)["accepted"] is False


def test_ack_with_wrong_sequence_is_rejected():
    g = sc.SynCookieGuard(queue_limit=0)
    cookie = g.receive_syn(syn(), 0.0)["cookie"]
    assert g.receive_ack(ack(cookie + 1), 1.0)["accepted"] is False


def test_cookie_restores_mss_and_window_scale():
    g = sc.SynCookieGuard(queue_limit=0)
    out = g.receive_syn(syn(options={"mss": 1400, "wscale": 5, "tsval": 77}), 0.0)
    res = g.receive_ack(ack(out["cookie"]), 1.0)
    assert res["accepted"] is True
    assert res["options"]["mss"] == 1400
    assert res["options"]["wscale"] == 5


def test_cookie_restores_timestamp():
    g = sc.SynCookieGuard(queue_limit=0)
    out = g.receive_syn(syn(options={"mss": 1460, "wscale": 7,
                                     "tsval": 123456789}), 0.0)
    res = g.receive_ack(ack(out["cookie"]), 1.0)
    assert res["accepted"] is True
    assert res["options"]["timestamp"] == 123456789


def test_old_key_is_accepted_during_rotation_overlap():
    g = sc.SynCookieGuard(queue_limit=0, rotate_interval=60.0, overlap=30.0)
    cookie = g.receive_syn(syn(), 0.0)["cookie"]
    g.rotate_key(10.0)
    assert g.receive_ack(ack(cookie), 15.0)["accepted"] is True


def test_half_open_timeout_is_configurable():
    g = sc.SynCookieGuard(queue_limit=10, timeout=120.0, sync_threshold=99)
    g.receive_syn(syn(src="1.1.1.1"), 0.0)
    g.receive_syn(syn(src="1.1.1.2"), 0.0)
    assert g.expire(60.0) == 0
    assert g.expire(121.0) == 2
    assert g.table_sizes()["ipv4"] == 0


def test_retransmit_interval_backs_off():
    g = sc.SynCookieGuard()
    assert g.retransmit_interval(0) == 1
    assert g.retransmit_interval(1) == 2
    assert g.retransmit_interval(2) == 4
    assert g.retransmit_interval(8) == 16


def test_normal_peak_with_acks_is_not_treated_as_attack():
    g = sc.SynCookieGuard(queue_limit=100, sync_threshold=4)
    for i in range(6):
        g.receive_syn(syn(src="10.0.0.%d" % (i + 1)), float(i) * 0.1)
    for i in range(6):
        g.receive_ack(ack(0, src="10.0.0.%d" % (i + 1)), 1.0)
    assert g.under_attack(1.0) is False


def test_syn_flood_without_acks_is_treated_as_attack():
    g = sc.SynCookieGuard(queue_limit=100, sync_threshold=4)
    for i in range(6):
        g.receive_syn(syn(src="10.0.0.%d" % (i + 1)), float(i) * 0.1)
    assert g.under_attack(1.0) is True


def test_cookie_mode_exits_after_quiet_period():
    g = sc.SynCookieGuard(queue_limit=100, sync_threshold=2, quiet_period=5.0)
    for i in range(4):
        g.receive_syn(syn(src="10.0.0.%d" % (i + 1)), float(i))
    assert g.mode(4.5) == "cookie"
    assert g.mode(8.1) == "normal"


def test_trusted_proxy_header_supplies_real_source():
    g = sc.SynCookieGuard(trusted_proxies=["10.0.0.0/8"])
    forwarded = syn(src="10.0.0.9")
    forwarded["proxy"] = "PROXY TCP4 203.0.113.7 10.0.0.9 5555 443\r\n"
    assert g.source_of(forwarded) == "203.0.113.7"
    direct = syn(src="192.0.2.10")
    direct["proxy"] = "PROXY TCP4 203.0.113.7 192.0.2.10 5555 443\r\n"
    assert g.source_of(direct) == "192.0.2.10"


def test_whitelisted_cidr_bypasses_cookie():
    g = sc.SynCookieGuard(queue_limit=0, whitelist=["203.0.113.0/24"])
    allowed = g.receive_syn(syn(src="203.0.113.9"), 0.0)
    assert allowed["action"] == "synack"
    assert allowed["cookie"] is None
    other = g.receive_syn(syn(src="198.51.100.9"), 0.0)
    assert other["action"] == "cookie_synack"


def test_ipv4_and_ipv6_use_separate_tables():
    g = sc.SynCookieGuard(queue_limit=10, sync_threshold=99)
    g.receive_syn(syn(family=4), 0.0)
    g.receive_syn(syn(src="2001:db8::1", family=6), 0.0)
    assert g.table_sizes() == {"ipv4": 1, "ipv6": 1}
    v4 = sc.SynCookieGuard(queue_limit=0).receive_syn(syn(family=4), 0.0)["cookie"]
    v6 = sc.SynCookieGuard(queue_limit=0).receive_syn(
        syn(src="2001:db8::1", family=6), 0.0)["cookie"]
    assert v4 != v6
