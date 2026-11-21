import hashlib
import ipaddress


class SynCookieError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_MAC_BITS = 64
_MAC_MASK = (1 << _MAC_BITS) - 1
_MSS_SHIFT = 64
_WSCALE_SHIFT = 80
_TSVAL_SHIFT = 84
_ISSUED_SHIFT = 116


def _parse_networks(entries):
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(str(entry), strict=False))
        except ValueError:
            continue
    return networks


def _in_networks(address, networks):
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return False
    return any(ip in network for network in networks)


class SynCookieGuard:
    def __init__(self, secret=b"cookie-secret", queue_limit=2, timeout=30.0,
                 rotate_interval=60.0, overlap=30.0, quiet_period=5.0,
                 sync_threshold=4, attack_window=5.0, max_retransmit=16.0,
                 whitelist=(), trusted_proxies=()):
        self.secret = secret
        self.queue_limit = queue_limit
        self.timeout = timeout
        self.rotate_interval = rotate_interval
        self.overlap = overlap
        self.quiet_period = quiet_period
        self.sync_threshold = sync_threshold
        self.attack_window = attack_window
        self.max_retransmit = max_retransmit
        self.whitelist = list(whitelist)
        self.trusted_proxies = list(trusted_proxies)
        self._whitelist_nets = _parse_networks(self.whitelist)
        self._proxy_nets = _parse_networks(self.trusted_proxies)
        self._tables = {4: {}, 6: {}}
        self._syn_times = []
        self._ack_times = []
        self._last_attack = None
        self._isn = 0
        self._current_key = secret
        self._old_keys = []
        self._last_rotate = 0.0
        self._cookies_issued = 0
        self._accepted = 0
        self._rejected = 0

    def receive_syn(self, packet, now=0.0):
        self._maybe_rotate(now)
        src = self.source_of(packet)
        self._syn_times.append(now)
        self._prune(self._syn_times, now)
        if self.under_attack(now):
            self._last_attack = now
        if _in_networks(src, self._whitelist_nets):
            return {"action": "synack", "cookie": None, "seq": self._next_isn()}
        table = self._tables[packet["family"]]
        if len(table) < self.queue_limit and self.mode(now) == "normal":
            isn = self._next_isn()
            key = (src, packet["sport"], packet["dst"], packet["dport"])
            table[key] = {"isn": isn, "time": now,
                          "options": dict(packet.get("options") or {})}
            return {"action": "synack", "cookie": None, "seq": isn}
        cookie = self._make_cookie(packet, src, now)
        self._cookies_issued += 1
        return {"action": "cookie_synack", "cookie": cookie, "seq": cookie}

    def receive_ack(self, packet, now=0.0):
        self._maybe_rotate(now)
        src = self.source_of(packet)
        self._ack_times.append(now)
        self._prune(self._ack_times, now)
        table = self._tables[packet["family"]]
        key = (src, packet["sport"], packet["dst"], packet["dport"])
        entry = table.pop(key, None)
        if entry is not None:
            self._accepted += 1
            return {"accepted": True, "options": entry["options"],
                    "source": src}
        options = self._validate_cookie(packet.get("ack"), packet, src, now)
        if options is not None:
            self._accepted += 1
            return {"accepted": True, "options": options, "source": src}
        self._rejected += 1
        return {"accepted": False, "source": src}

    def expire(self, now):
        removed = 0
        for table in self._tables.values():
            stale = [key for key, entry in table.items()
                     if now - entry["time"] > self.timeout]
            for key in stale:
                del table[key]
                removed += 1
        return removed

    def rotate_key(self, now):
        seed = (self._current_key + repr(now).encode("utf-8")
                + repr(len(self._old_keys)).encode("utf-8"))
        new_key = hashlib.blake2b(seed, digest_size=32).digest()
        self._old_keys.append((now + self.overlap, self._current_key))
        self._current_key = new_key
        self._last_rotate = now

    def retransmit_interval(self, attempt):
        attempt = max(0, int(attempt))
        return min(2 ** attempt, int(self.max_retransmit))

    def stats(self):
        return {
            "syns": len(self._syn_times),
            "acks": len(self._ack_times),
            "cookies_issued": self._cookies_issued,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "half_open_ipv4": len(self._tables[4]),
            "half_open_ipv6": len(self._tables[6]),
        }

    def under_attack(self, now):
        syns, acks = self._window_counts(now)
        return syns > self.sync_threshold and acks * 2 < syns

    def mode(self, now):
        if self.under_attack(now):
            return "cookie"
        if (self._last_attack is not None
                and now - self._last_attack < self.quiet_period):
            return "cookie"
        return "normal"

    def source_of(self, packet):
        src = packet.get("src")
        header = packet.get("proxy")
        if header and _in_networks(src, self._proxy_nets):
            parts = str(header).strip().split()
            if len(parts) >= 3 and parts[0] == "PROXY":
                return parts[2]
        return src

    def table_sizes(self):
        return {"ipv4": len(self._tables[4]), "ipv6": len(self._tables[6])}

    def _next_isn(self):
        self._isn += 1
        return self._isn

    def _prune(self, times, now):
        horizon = now - self.attack_window
        del times[:len([t for t in times if t <= horizon])]

    def _window_counts(self, now):
        horizon = now - self.attack_window
        syns = sum(1 for t in self._syn_times if horizon < t <= now)
        acks = sum(1 for t in self._ack_times if horizon < t <= now)
        return syns, acks

    def _maybe_rotate(self, now):
        if now - self._last_rotate >= self.rotate_interval:
            self.rotate_key(now)

    def _valid_keys(self, now):
        yield self._current_key
        for retired_until, key in self._old_keys:
            if now <= retired_until:
                yield key

    def _mac(self, key, src, packet, mss, wscale, tsval, issued):
        material = "|".join(str(part) for part in (
            issued, packet["family"], src, packet["sport"], packet["dst"],
            packet["dport"], mss, wscale, tsval)).encode("utf-8")
        digest = hashlib.blake2b(material, digest_size=8,
                                 key=key[:64]).digest()
        return int.from_bytes(digest, "big")

    def _make_cookie(self, packet, src, now):
        options = packet.get("options") or {}
        mss = int(options.get("mss", 0)) & 0xFFFF
        wscale = int(options.get("wscale", 0)) & 0xF
        tsval = int(options.get("tsval", 0)) & 0xFFFFFFFF
        issued = int(now) & 0xFFFFFFFF
        mac = self._mac(self._current_key, src, packet, mss, wscale, tsval,
                        issued)
        return (mac | (mss << _MSS_SHIFT) | (wscale << _WSCALE_SHIFT)
                | (tsval << _TSVAL_SHIFT) | (issued << _ISSUED_SHIFT))

    def _validate_cookie(self, cookie, packet, src, now):
        try:
            cookie = int(cookie)
        except (TypeError, ValueError):
            return None
        if cookie <= 0:
            return None
        mac = cookie & _MAC_MASK
        mss = (cookie >> _MSS_SHIFT) & 0xFFFF
        wscale = (cookie >> _WSCALE_SHIFT) & 0xF
        tsval = (cookie >> _TSVAL_SHIFT) & 0xFFFFFFFF
        issued = (cookie >> _ISSUED_SHIFT) & 0xFFFFFFFF
        age = now - issued
        if age < -1.0 or age > max(self.timeout, 1.0):
            return None
        for key in self._valid_keys(now):
            if self._mac(key, src, packet, mss, wscale, tsval,
                         issued) == mac:
                return {"mss": mss, "wscale": wscale, "timestamp": tsval}
        return None
