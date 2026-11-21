import struct

import pytest

import proxy_header_trust as pt

SIG = b"\r\n\r\n\x00\r\nQUIT\n"
CLIENT_HELLO = b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc"


def crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x82F63B78
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


def v1(src="192.0.2.5", dst="198.51.100.7", sport=40000, dport=443, proto="TCP4"):
    return ("PROXY %s %s %s %d %d\r\n" % (proto, src, dst, sport, dport)).encode()


def v2(family=0x11, cmd=0x21, src=b"\x0a\x00\x00\x05", dst=b"\xc0\x00\x02\x01",
       sport=40000, dport=443, tlvs=b"", length=None):
    body = src + dst + struct.pack("!HH", sport, dport) + tlvs
    if length is None:
        length = len(body)
    return SIG + bytes([cmd, family]) + struct.pack("!H", length) + body


def v2_unspec():
    return SIG + bytes([0x21, 0x00]) + struct.pack("!H", 0)


def v2_with_crc(**kwargs):
    base = v2(tlvs=b"\x03\x00\x04\x00\x00\x00\x00", **kwargs)
    value = crc32c(base)
    return base[:-4] + struct.pack("!I", value), value


def test_untrusted_peer_is_rejected():
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().accept("c1", "203.0.113.9", v1())
    assert exc.value.code == "untrusted_peer"


def test_trusted_peer_accepts_v1_header():
    trust = pt.ProxyHeaderTrust()
    info = trust.accept("c1", "10.0.0.1", v1())
    assert info["version"] == 1
    assert info["command"] == "PROXY"
    assert info["src"] == "192.0.2.5"
    assert info["dst"] == "198.51.100.7"
    assert info["sport"] == 40000
    assert info["dport"] == 443
    assert info["consumed"] == len(v1())
    assert trust.source("c1")["src"] == "192.0.2.5"


def test_v2_binary_header_is_parsed():
    header = v2()
    info = pt.ProxyHeaderTrust().parse("10.0.0.1", header)
    assert info["version"] == 2
    assert info["family"] == "INET"
    assert info["src"] == "10.0.0.5"
    assert info["dst"] == "192.0.2.1"
    assert info["sport"] == 40000
    assert info["dport"] == 443
    assert info["consumed"] == len(header)


def test_local_command_has_no_addresses():
    info = pt.ProxyHeaderTrust().parse("10.0.0.1", b"PROXY UNKNOWN\r\n")
    assert info["command"] == "LOCAL"
    assert info["src"] is None
    assert info["sport"] is None


def test_unspec_family_has_no_addresses():
    info = pt.ProxyHeaderTrust().parse("10.0.0.1", v2_unspec())
    assert info["family"] == "UNSPEC"
    assert info["src"] is None
    assert info["dport"] is None


def test_length_beyond_buffer_is_rejected():
    header = SIG + bytes([0x21, 0x11]) + struct.pack("!H", 12) + b"\x0a\x00\x00\x05"
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().parse("10.0.0.1", header)
    assert exc.value.code == "short_header"


def test_malformed_tlv_is_rejected():
    header = v2(tlvs=b"\x03\x00\x09\x00")
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().parse("10.0.0.1", header)
    assert exc.value.code == "bad_tlv"


def test_valid_crc32c_is_accepted():
    header, value = v2_with_crc()
    info = pt.ProxyHeaderTrust().parse("10.0.0.1", header)
    assert info["src"] == "10.0.0.5"
    assert info["tlvs"]["crc32c"] == value


def test_bad_crc32c_is_rejected():
    header, value = v2_with_crc()
    broken = header[:-4] + struct.pack("!I", value ^ 0x1)
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().parse("10.0.0.1", broken)
    assert exc.value.code == "bad_crc32c"


def test_multiple_headers_are_rejected():
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().parse("10.0.0.1", v1() + v2())
    assert exc.value.code == "multiple_headers"


def test_zero_source_port_is_rejected():
    with pytest.raises(pt.ProxyHeaderError) as exc:
        pt.ProxyHeaderTrust().parse("10.0.0.1", v1(sport=0))
    assert exc.value.code == "bad_port"


def test_consumed_offset_leaves_client_hello():
    header = v1()
    data = header + CLIENT_HELLO
    info = pt.ProxyHeaderTrust().parse("10.0.0.1", data)
    assert info["consumed"] == len(header)
    assert data[info["consumed"]:] == CLIENT_HELLO


def test_connection_rebind_is_rejected():
    trust = pt.ProxyHeaderTrust()
    trust.accept("c1", "10.0.0.1", v1(src="192.0.2.5"))
    with pytest.raises(pt.ProxyHeaderError) as exc:
        trust.accept("c1", "10.0.0.1", v2())
    assert exc.value.code == "rebind"
    assert trust.source("c1")["src"] == "192.0.2.5"


def test_missing_header_does_not_fall_back():
    trust = pt.ProxyHeaderTrust()
    with pytest.raises(pt.ProxyHeaderError) as exc:
        trust.accept("c1", "10.0.0.1", CLIENT_HELLO)
    assert exc.value.code == "missing_header"
    assert trust.source("c1") is None


def test_sources_are_bound_per_connection():
    trust = pt.ProxyHeaderTrust()
    trust.accept("c1", "10.0.0.1", v1(src="192.0.2.5", sport=1111))
    trust.accept("c2", "10.0.0.2", v1(src="198.51.100.9", sport=2222))
    assert trust.source("c1")["src"] == "192.0.2.5"
    assert trust.source("c1")["sport"] == 1111
    assert trust.source("c2")["src"] == "198.51.100.9"
    assert trust.source("c2")["sport"] == 2222
