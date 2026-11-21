import copy
import ipaddress
import math
import re
import threading
from collections.abc import Mapping


_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")
_MAX_VNI = (1 << 24) - 1
_VXLAN_OVERHEAD = 50


def _validate_vni(vni):
    if isinstance(vni, bool) or not isinstance(vni, int):
        raise ValueError("vni must be an integer")
    if not 1 <= vni <= _MAX_VNI:
        raise ValueError("vni must be between 1 and %d" % _MAX_VNI)
    return vni


def _normalize_mac(mac):
    if not isinstance(mac, str) or not _MAC_RE.fullmatch(mac):
        raise ValueError("invalid MAC address")
    return mac.replace("-", ":").lower()


def _validate_vtep(vtep):
    if not isinstance(vtep, str) or not vtep:
        raise ValueError("vtep must be a non-empty string")
    return vtep


def _validate_seq(seq):
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("seq must be a non-negative integer")
    return seq


def _validate_clock(now):
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("clock value must be numeric")
    if not math.isfinite(now) or now < 0:
        raise ValueError("clock value must be finite and non-negative")
    return now


def _normalize_ip(ip):
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError as exc:
        raise ValueError("invalid IP address") from exc


class FdbLearner:
    def __init__(self, age_seconds=300, arp_ttl=60):
        if (
            isinstance(age_seconds, bool)
            or not isinstance(age_seconds, (int, float))
            or not math.isfinite(age_seconds)
            or age_seconds < 0
        ):
            raise ValueError("age_seconds must be a finite non-negative number")
        if (
            isinstance(arp_ttl, bool)
            or not isinstance(arp_ttl, (int, float))
            or not math.isfinite(arp_ttl)
            or arp_ttl < 0
        ):
            raise ValueError("arp_ttl must be a finite non-negative number")

        self.age_seconds = age_seconds
        self.arp_ttl = arp_ttl
        self.fdb = {}
        self.vteps = {}
        self.arp = {}
        self.lock = threading.RLock()

    def _expired(self, entry, now):
        return not entry["static"] and now - entry["updated_at"] > self.age_seconds

    def join_vtep(self, vni, vtep):
        vni = _validate_vni(vni)
        vtep = _validate_vtep(vtep)
        with self.lock:
            members = self.vteps.setdefault(vni, {})
            if vtep in members:
                return False
            members[vtep] = None
            return True

    def learn(self, vni, mac, vtep, seq, now, static=False):
        vni = _validate_vni(vni)
        mac = _normalize_mac(mac)
        vtep = _validate_vtep(vtep)
        seq = _validate_seq(seq)
        now = _validate_clock(now)
        static = bool(static)
        key = (vni, mac)

        with self.lock:
            current = self.fdb.get(key)
            if current is None:
                self.fdb[key] = {
                    "vni": vni,
                    "mac": mac,
                    "vtep": vtep,
                    "seq": seq,
                    "updated_at": now,
                    "static": static,
                }
                return {"updated": True, "reason": "learned"}

            if current["static"] and not static:
                if current["vtep"] != vtep:
                    return {"updated": False, "reason": "static_entry"}
                if seq < current["seq"]:
                    return {"updated": False, "reason": "stale_sequence"}
                current["updated_at"] = now
                return {"updated": True, "reason": "refreshed"}

            if current["vtep"] == vtep:
                if seq < current["seq"]:
                    return {"updated": False, "reason": "stale_sequence"}
                current["seq"] = seq
                current["updated_at"] = now
                if static and not current["static"]:
                    current["static"] = True
                    return {"updated": True, "reason": "promoted_static"}
                return {"updated": True, "reason": "refreshed"}

            if seq <= current["seq"]:
                return {"updated": False, "reason": "stale_sequence"}

            current.update(
                {
                    "vtep": vtep,
                    "seq": seq,
                    "updated_at": now,
                    "static": static or current["static"],
                }
            )
            return {"updated": True, "reason": "migrated"}

    def lookup(self, vni, mac, now):
        vni = _validate_vni(vni)
        mac = _normalize_mac(mac)
        now = _validate_clock(now)
        with self.lock:
            entry = self.fdb.get((vni, mac))
            if entry is None or self._expired(entry, now):
                return None
            return copy.deepcopy(entry)

    def age(self, now):
        now = _validate_clock(now)
        removed = 0
        with self.lock:
            for key, entry in list(self.fdb.items()):
                if self._expired(entry, now):
                    del self.fdb[key]
                    removed += 1
        return removed

    def bum_replicators(self, vni, source_vtep):
        vni = _validate_vni(vni)
        source_vtep = _validate_vtep(source_vtep)
        with self.lock:
            return [vtep for vtep in self.vteps.get(vni, {}) if vtep != source_vtep]

    def learn_arp(self, vni, ip, mac, now):
        vni = _validate_vni(vni)
        ip = _normalize_ip(ip)
        mac = _normalize_mac(mac)
        now = _validate_clock(now)
        with self.lock:
            self.arp[(vni, ip)] = {"vni": vni, "ip": ip, "mac": mac, "updated_at": now}

    def resolve_arp(self, vni, ip, now):
        vni = _validate_vni(vni)
        ip = _normalize_ip(ip)
        now = _validate_clock(now)
        key = (vni, ip)
        with self.lock:
            entry = self.arp.get(key)
            if entry is None:
                return None
            if now - entry["updated_at"] > self.arp_ttl:
                del self.arp[key]
                return None
            return entry["mac"]

    def validate_encap(self, packet):
        if not isinstance(packet, Mapping):
            raise ValueError("encapsulation metadata must be a mapping")
        checksum = packet.get("udp_checksum")
        if (
            isinstance(checksum, bool)
            or not isinstance(checksum, int)
            or not 1 <= checksum <= 0xFFFF
        ):
            raise ValueError("outer UDP checksum is missing or invalid")
        return True

    def mtu_for(self, underlay_mtu=None):
        if underlay_mtu is None:
            underlay_mtu = 1500
        if (
            isinstance(underlay_mtu, bool)
            or not isinstance(underlay_mtu, int)
            or underlay_mtu <= _VXLAN_OVERHEAD
        ):
            raise ValueError("underlay MTU must be greater than VXLAN overhead")
        return underlay_mtu - _VXLAN_OVERHEAD

    def detect_loop(self, vtep_path):
        if vtep_path is None:
            return False
        try:
            path = list(vtep_path)
        except TypeError as exc:
            raise ValueError("vtep_path must be iterable") from exc
        seen = set()
        for vtep in path:
            vtep = _validate_vtep(vtep)
            if vtep in seen:
                return True
            seen.add(vtep)
        return False

    def parse_evpn_type2(self, payload):
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError("EVPN type-2 payload must be bytes-like")
        payload = bytes(payload)
        if len(payload) < 10:
            raise ValueError("truncated EVPN type-2 route")
        mac_length_bits = payload[0]
        if mac_length_bits != 48:
            raise ValueError("invalid MAC length in EVPN type-2 route")
        mac = ":".join("%02x" % octet for octet in payload[1:7])
        vni = int.from_bytes(payload[7:10], "big")
        if not 1 <= vni <= _MAX_VNI:
            raise ValueError("invalid VNI in EVPN type-2 route")
        return {"mac": mac, "vni": vni}

    def sync_controller(self, vni, routes):
        vni = _validate_vni(vni)
        if routes is None:
            raise ValueError("routes must be iterable")

        prepared = {}
        try:
            route_iter = iter(routes)
        except TypeError as exc:
            raise ValueError("routes must be iterable") from exc

        for route in route_iter:
            if not isinstance(route, Mapping):
                raise ValueError("controller route must be a mapping")
            if "mac" not in route or "vtep" not in route:
                raise ValueError("controller route requires mac and vtep")
            if "vni" in route and _validate_vni(route["vni"]) != vni:
                raise ValueError("controller route VNI does not match target VNI")

            mac = _normalize_mac(route["mac"])
            vtep = _validate_vtep(route["vtep"])
            seq = route.get("seq")
            if seq is not None:
                seq = _validate_seq(seq)
            prepared[(vni, mac)] = (mac, vtep, seq)

        with self.lock:
            for key, (mac, vtep, seq) in prepared.items():
                current = self.fdb.get(key)
                if current is not None and current["static"]:
                    continue

                if current is None:
                    self.fdb[key] = {
                        "vni": vni,
                        "mac": mac,
                        "vtep": vtep,
                        "seq": 0 if seq is None else seq,
                        "updated_at": 0,
                        "static": False,
                    }
                elif seq is None or seq > current["seq"]:
                    current.update(
                        {
                            "vtep": vtep,
                            "seq": current["seq"] if seq is None else seq,
                        }
                    )

            for key, entry in list(self.fdb.items()):
                if entry["vni"] == vni and not entry["static"] and key not in prepared:
                    del self.fdb[key]

    def snapshot(self):
        with self.lock:
            per_vni = {}
            for entry in self.fdb.values():
                per_vni[entry["vni"]] = per_vni.get(entry["vni"], 0) + 1
            return {
                "entries": len(self.fdb),
                "static": sum(1 for entry in self.fdb.values() if entry["static"]),
                "vnis": per_vni,
                "arp_entries": len(self.arp),
                "vteps": {vni: len(members) for vni, members in self.vteps.items()},
                "fdb": {key: copy.deepcopy(entry) for key, entry in self.fdb.items()},
                "arp": {key: copy.deepcopy(entry) for key, entry in self.arp.items()},
            }
