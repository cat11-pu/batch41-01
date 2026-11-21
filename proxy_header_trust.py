import ipaddress
import struct


V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"
V1_SIGNATURE = b"PROXY "

_FAMILIES = {
    0x0: "UNSPEC",
    0x1: "INET",
    0x2: "INET6",
    0x3: "UNIX",
}

_TRANSPORTS = {
    0x0: "UNSPEC",
    0x1: "STREAM",
    0x2: "DGRAM",
}

_CRC32C_POLY = 0x82F63B78


def _crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _CRC32C_POLY
            else:
                crc >>= 1
    return crc ^ 0xFFFFFFFF


class ProxyHeaderError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ProxyHeaderTrust:
    def __init__(self, trusted=("10.0.0.0/8", "127.0.0.1/32")):
        self.trusted = [self._coerce_network(item) for item in trusted]
        self._sources = {}

    @staticmethod
    def _coerce_network(item):
        if isinstance(item, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            return item
        return ipaddress.ip_network(str(item), strict=False)

    @staticmethod
    def _peer_address(peer):
        if isinstance(peer, (tuple, list)):
            peer = peer[0]
        return ipaddress.ip_address(str(peer))

    def _require_trusted(self, peer):
        try:
            address = self._peer_address(peer)
        except ValueError as exc:
            raise ProxyHeaderError("untrusted_peer", str(peer)) from exc

        for network in self.trusted:
            if address.version == network.version and address in network:
                return
        raise ProxyHeaderError("untrusted_peer", str(peer))

    def parse(self, peer, data):
        self._require_trusted(peer)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ProxyHeaderError("missing_header")

        buffer = bytes(data)
        if not buffer:
            raise ProxyHeaderError("missing_header")

        info = self._dispatch(buffer)
        self._reject_trailing_header(buffer, info["consumed"])
        return info

    def _dispatch(self, buffer):
        if buffer.startswith(V2_SIGNATURE):
            return self._parse_v2(buffer)
        if buffer.startswith(V1_SIGNATURE):
            return self._parse_v1(buffer)

        if len(buffer) < len(V2_SIGNATURE) and V2_SIGNATURE.startswith(buffer):
            raise ProxyHeaderError("short_header")
        if len(buffer) < len(V1_SIGNATURE) and V1_SIGNATURE.startswith(buffer):
            raise ProxyHeaderError("short_header")
        raise ProxyHeaderError("missing_header")

    @staticmethod
    def _reject_trailing_header(buffer, consumed):
        remainder = buffer[consumed:]
        if remainder.startswith(V2_SIGNATURE) or remainder.startswith(V1_SIGNATURE):
            raise ProxyHeaderError("multiple_headers")

    @staticmethod
    def _port(value):
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ProxyHeaderError("bad_port", value) from exc
        if not 0 < port <= 0xFFFF:
            raise ProxyHeaderError("bad_port", value)
        return port

    def _parse_v1(self, buffer):
        line_end = buffer.find(b"\r\n")
        if line_end < 0:
            raise ProxyHeaderError("short_header")

        consumed = line_end + 2
        fields = buffer[:line_end].split(b" ")
        if len(fields) < 2 or fields[0] != b"PROXY":
            raise ProxyHeaderError("bad_header")

        protocol = fields[1].upper()
        if protocol == b"UNKNOWN":
            return {
                "version": 1,
                "command": "LOCAL",
                "family": "UNSPEC",
                "transport": "UNSPEC",
                "src": None,
                "dst": None,
                "sport": None,
                "dport": None,
                "consumed": consumed,
                "tlvs": {},
            }

        if protocol not in (b"TCP4", b"TCP6") or len(fields) != 6:
            raise ProxyHeaderError("bad_header")

        try:
            src = ipaddress.ip_address(fields[2].decode("ascii"))
            dst = ipaddress.ip_address(fields[3].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProxyHeaderError("bad_address") from exc

        expected_version = 4 if protocol == b"TCP4" else 6
        if src.version != expected_version or dst.version != expected_version:
            raise ProxyHeaderError("bad_address")

        sport = self._port(fields[4])
        dport = self._port(fields[5])
        return {
            "version": 1,
            "command": "PROXY",
            "family": "INET" if expected_version == 4 else "INET6",
            "transport": "STREAM",
            "src": str(src),
            "dst": str(dst),
            "sport": sport,
            "dport": dport,
            "consumed": consumed,
            "tlvs": {},
        }

    def _parse_v2(self, buffer):
        fixed_size = len(V2_SIGNATURE) + 4
        if len(buffer) < fixed_size:
            raise ProxyHeaderError("short_header")

        version_command = buffer[12]
        family_transport = buffer[13]
        declared_length = struct.unpack("!H", buffer[14:16])[0]
        total_size = fixed_size + declared_length

        if total_size > len(buffer):
            raise ProxyHeaderError("short_header")

        version = version_command >> 4
        command = version_command & 0x0F
        if version != 2:
            raise ProxyHeaderError("bad_version")
        if command not in (0, 1):
            raise ProxyHeaderError("bad_command")

        address_family = family_transport >> 4
        transport = family_transport & 0x0F
        if address_family not in _FAMILIES or transport not in _TRANSPORTS:
            raise ProxyHeaderError("bad_family")

        body = buffer[fixed_size:total_size]
        info = {
            "version": 2,
            "command": "LOCAL" if command == 0 else "PROXY",
            "family": _FAMILIES[address_family],
            "transport": _TRANSPORTS[transport],
            "src": None,
            "dst": None,
            "sport": None,
            "dport": None,
            "consumed": total_size,
            "tlvs": {},
        }

        if command == 0:
            return info

        tlv_offset = 0
        if address_family == 1:
            if len(body) < 12:
                raise ProxyHeaderError("bad_length")
            try:
                src = ipaddress.ip_address(body[0:4])
                dst = ipaddress.ip_address(body[4:8])
            except ValueError as exc:
                raise ProxyHeaderError("bad_address") from exc
            sport, dport = struct.unpack("!HH", body[8:12])
            info.update(src=str(src), dst=str(dst), sport=sport, dport=dport)
            tlv_offset = 12
        elif address_family == 2:
            if len(body) < 36:
                raise ProxyHeaderError("bad_length")
            try:
                src = ipaddress.ip_address(body[0:16])
                dst = ipaddress.ip_address(body[16:32])
            except ValueError as exc:
                raise ProxyHeaderError("bad_address") from exc
            sport, dport = struct.unpack("!HH", body[32:36])
            info.update(src=str(src), dst=str(dst), sport=sport, dport=dport)
            tlv_offset = 36
        elif address_family == 3:
            if len(body) < 216:
                raise ProxyHeaderError("bad_length")
            info["src"] = self._unix_path(body[0:108])
            info["dst"] = self._unix_path(body[108:216])
            tlv_offset = 216

        if address_family in (1, 2):
            if info["sport"] == 0 or info["dport"] == 0:
                raise ProxyHeaderError("bad_port")

        info["tlvs"] = self._parse_tlvs(
            buffer, body[tlv_offset:], fixed_size + tlv_offset, total_size
        )
        return info

    @staticmethod
    def _unix_path(raw):
        path = raw.split(b"\x00", 1)[0]
        return path.decode("utf-8", "surrogateescape")

    def _parse_tlvs(self, header, payload, payload_offset, total_size):
        tlvs = {}
        position = 0
        while position < len(payload):
            if len(payload) - position < 3:
                raise ProxyHeaderError("bad_tlv")

            tlv_type = payload[position]
            tlv_length = struct.unpack("!H", payload[position + 1:position + 3])[0]
            value_start = position + 3
            value_end = value_start + tlv_length
            if value_end > len(payload):
                raise ProxyHeaderError("bad_tlv")

            value = payload[value_start:value_end]
            absolute_start = payload_offset + position
            if tlv_type == 0x03:
                if tlv_length != 4:
                    raise ProxyHeaderError("bad_tlv")
                supplied = struct.unpack("!I", value)[0]
                crc_message = bytearray(header[:total_size])
                crc_message[absolute_start + 3:absolute_start + 7] = b"\x00\x00\x00\x00"
                if _crc32c(crc_message) != supplied:
                    raise ProxyHeaderError("bad_crc32c")
                tlvs["crc32c"] = supplied
            elif tlv_type == 0x20:
                tlvs["ssl"] = self._parse_ssl_tlv(value)
            elif tlv_type == 0x01:
                tlvs["alpn"] = bytes(value)
            elif tlv_type == 0x02:
                tlvs["authority"] = bytes(value)
            elif tlv_type == 0x05:
                tlvs["unique_id"] = bytes(value)
            else:
                tlvs.setdefault("other", []).append((tlv_type, bytes(value)))

            position = value_end

        return tlvs

    @staticmethod
    def _parse_ssl_tlv(value):
        parsed = {}
        position = 0
        while position < len(value):
            if len(value) - position < 3:
                raise ProxyHeaderError("bad_tlv")

            ssl_type = value[position]
            ssl_length = struct.unpack("!H", value[position + 1:position + 3])[0]
            value_start = position + 3
            value_end = value_start + ssl_length
            if value_end > len(value):
                raise ProxyHeaderError("bad_tlv")

            chunk = bytes(value[value_start:value_end])
            if ssl_type == 0x21:
                parsed["client"] = chunk
            elif ssl_type == 0x22:
                parsed["server"] = chunk
            else:
                parsed.setdefault("other", []).append((ssl_type, chunk))

            position = value_end

        return parsed

    def accept(self, conn_id, peer, data):
        self._require_trusted(peer)
        if conn_id in self._sources:
            buffer = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""
            if buffer.startswith(V2_SIGNATURE) or buffer.startswith(V1_SIGNATURE):
                raise ProxyHeaderError("rebind", conn_id)
            raise ProxyHeaderError("missing_header", conn_id)

        info = self.parse(peer, data)
        self._sources[conn_id] = info
        return info

    def source(self, conn_id):
        info = self._sources.get(conn_id)
        return dict(info) if info is not None else None

    def reset(self, conn_id):
        self._sources.pop(conn_id, None)
