import ipaddress
import logging
import threading
import uuid


LOG = logging.getLogger("turn_allocation_guard")

# RFC 5766: only UDP relaying is supported here.
ALLOWED_TRANSPORTS = frozenset({"udp"})

# RFC 5766 section 11: valid channel numbers are 0x4000..0x7FFF.
CHANNEL_NUMBER_MIN = 0x4000
CHANNEL_NUMBER_MAX = 0x7FFF

RELAY_PORT_MAX = 65535


class AllocationGuard:
    """State machine guarding TURN allocations, permissions and channel binds.

    Every mutating entry point holds ``self._lock`` so that a five-tuple can
    never get two allocations through concurrent requests, and all validation
    failures raise :class:`ValueError` after emitting an audit log entry.
    """

    def __init__(self, realm="turn.example.com", nonce_ttl=300, max_lifetime=3600,
                 permission_ttl=300, relay_port_start=49152):
        self.realm = realm
        self.nonce_ttl = nonce_ttl
        self.max_lifetime = max_lifetime
        self.permission_ttl = permission_ttl
        self.relay_port_start = relay_port_start
        self.nonces = {}
        self.allocations = {}
        self.by_tuple = {}
        self.next_port = relay_port_start
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _reject(self, reason, **context):
        """Audit every refused request, then raise."""
        LOG.warning("turn request rejected: %s %s", reason, context)
        raise ValueError(reason)

    @staticmethod
    def _parse_ip(value):
        try:
            return ipaddress.ip_address(value)
        except (ValueError, TypeError):
            return None

    def _assert_public_peer(self, peer_ip):
        """Block loopback, link-local, private and other internal peers.

        This is the SSRF guard that stops the relay from being turned into a
        tunnel into internal networks (including the cloud metadata address).
        """
        address = self._parse_ip(peer_ip)
        if address is None:
            self._reject("invalid peer ip", peer_ip=peer_ip)
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_multicast or address.is_unspecified or address.is_reserved):
            self._reject("peer address is not publicly reachable", peer_ip=peer_ip)
        # Carrier-grade NAT range 100.64.0.0/10 is operator-internal space.
        if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
                "100.64.0.0/10"):
            self._reject("peer address is not publicly reachable", peer_ip=peer_ip)
        return address

    def _validate_xor_mapped_address(self, attribute):
        if not isinstance(attribute, dict):
            self._reject("missing XOR-MAPPED-ADDRESS")
        family = attribute.get("family")
        length = attribute.get("address_length")
        address = self._parse_ip(attribute.get("ip"))
        if family == 1:
            expected_length, expected_version = 4, 4
        elif family == 2:
            expected_length, expected_version = 16, 6
        else:
            self._reject("unknown XOR-MAPPED-ADDRESS family", family=family)
        if length != expected_length:
            self._reject("XOR-MAPPED-ADDRESS length does not match family",
                         family=family, address_length=length)
        if address is None or address.version != expected_version:
            self._reject("XOR-MAPPED-ADDRESS ip does not match family",
                         family=family, ip=attribute.get("ip"))
        port = attribute.get("port")
        if not isinstance(port, int) or not (0 < port <= RELAY_PORT_MAX):
            self._reject("XOR-MAPPED-ADDRESS port is invalid", port=port)

    def _consume_nonce(self, nonce, now):
        issued_at = self.nonces.get(nonce)
        if issued_at is None:
            self._reject("unknown nonce", nonce=nonce)
        if now - issued_at > self.nonce_ttl:
            del self.nonces[nonce]
            self._reject("stale nonce", nonce=nonce)
        # Single use: remove before the allocation is created so the same
        # nonce can never be replayed, even by concurrent requests.
        del self.nonces[nonce]

    def _allocation(self, allocation_id):
        allocation = self.allocations.get(allocation_id)
        if allocation is None:
            self._reject("unknown allocation", allocation_id=allocation_id)
        return allocation

    # ------------------------------------------------------------------
    # STUN/TURN operations
    # ------------------------------------------------------------------
    def issue_nonce(self, now):
        with self._lock:
            expired = [nonce for nonce, issued_at in self.nonces.items()
                       if now - issued_at > self.nonce_ttl]
            for nonce in expired:
                del self.nonces[nonce]
            nonce = uuid.uuid4().hex
            self.nonces[nonce] = now
            return {"nonce": nonce, "realm": self.realm, "issued_at": now}

    def allocate(self, request, now):
        with self._lock:
            # Framing / integrity checks first, before any state is touched.
            if request.get("length", 0) > request.get("actual_length", 0):
                self._reject("declared message length exceeds actual payload",
                             length=request.get("length"),
                             actual_length=request.get("actual_length"))
            if not request.get("fingerprint_ok", False):
                self._reject("FINGERPRINT validation failed")
            self._validate_xor_mapped_address(request.get("xor_mapped_address"))

            transport = request.get("transport")
            if transport not in ALLOWED_TRANSPORTS:
                self._reject("only UDP relay transport is supported", transport=transport)

            requested_lifetime = request.get("requested_lifetime")
            if not isinstance(requested_lifetime, int) or requested_lifetime <= 0:
                self._reject("requested lifetime must be a positive integer",
                             requested_lifetime=requested_lifetime)
            lifetime = min(requested_lifetime, self.max_lifetime)

            self._consume_nonce(request.get("nonce"), now)

            five_tuple = (
                request.get("client_ip"),
                request.get("client_port"),
                request.get("transport"),
            )
            if five_tuple in self.by_tuple:
                self._reject("allocation for five-tuple already exists",
                             five_tuple=five_tuple)

            if self.next_port > RELAY_PORT_MAX:
                self._reject("relay port pool exhausted")
            relay_port = self.next_port
            self.next_port += 1

            allocation_id = uuid.uuid4().hex
            allocation = {
                "allocation_id": allocation_id,
                "five_tuple": five_tuple,
                "transport": transport,
                "relay_port": relay_port,
                "created_at": now,
                "expires_at": now + lifetime,
                "permissions": {},
                "channels": {},
            }
            self.allocations[allocation_id] = allocation
            self.by_tuple[five_tuple] = allocation_id
            return {
                "allocation_id": allocation_id,
                "lifetime": lifetime,
                "transport": transport,
                "relay_port": relay_port,
                "expires_at": allocation["expires_at"],
            }

    def create_permission(self, allocation_id, peer_ip, now):
        with self._lock:
            allocation = self._allocation(allocation_id)
            self._assert_public_peer(peer_ip)
            allocation["permissions"][peer_ip] = now + self.permission_ttl
            return {"peer_ip": peer_ip, "expires_at": now + self.permission_ttl}

    def channel_bind(self, allocation_id, channel, peer_ip, now):
        with self._lock:
            allocation = self._allocation(allocation_id)
            if not isinstance(channel, int) or not (
                    CHANNEL_NUMBER_MIN <= channel <= CHANNEL_NUMBER_MAX):
                self._reject("channel number out of valid range 0x4000..0x7FFF",
                             channel=channel)
            self._assert_public_peer(peer_ip)

            existing_peer = allocation["channels"].get(channel)
            if existing_peer is not None and existing_peer != peer_ip:
                self._reject("channel number already bound to another peer",
                             channel=channel, existing_peer=existing_peer,
                             requested_peer=peer_ip)
            allocation["channels"][channel] = peer_ip
            return {"channel": channel, "peer_ip": peer_ip, "bound_at": now}

    def send(self, allocation_id, peer_ip, now):
        with self._lock:
            allocation = self.allocations.get(allocation_id)
            if allocation is None or now >= allocation["expires_at"]:
                return False
            expires_at = allocation["permissions"].get(peer_ip)
            if expires_at is None:
                return False
            if now >= expires_at:
                del allocation["permissions"][peer_ip]
                return False
            return True

    def expire(self, now):
        with self._lock:
            # Sweep every bucket; a partial scan would leak relay ports.
            expired_ids = [
                allocation_id
                for allocation_id, allocation in list(self.allocations.items())
                if now >= allocation["expires_at"]
            ]
            for allocation_id in expired_ids:
                allocation = self.allocations.pop(allocation_id)
                self.by_tuple.pop(allocation["five_tuple"], None)

            stale_nonces = [nonce for nonce, issued_at in list(self.nonces.items())
                            if now - issued_at > self.nonce_ttl]
            for nonce in stale_nonces:
                del self.nonces[nonce]
            return len(expired_ids)

    def ice_role(self, local_role, remote_role, local_tie, remote_tie):
        """Resolve an ICE role agreement per RFC 5245 tie-breaker rules."""
        if local_role != remote_role:
            return local_role
        local_wins = local_tie > remote_tie
        if local_role == "controlling":
            return "controlling" if local_wins else "controlled"
        # Both claim controlled: the higher tie breaker switches to
        # controlling so the pair can always converge.
        return "controlling" if local_wins else "controlled"

    def snapshot(self):
        with self._lock:
            return {
                "allocations": len(self.allocations),
                "five_tuples": len(self.by_tuple),
                "active_nonces": len(self.nonces),
            }
