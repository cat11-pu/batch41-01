"""ACME order, challenge, and certificate renewal state machine.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the state machine is implemented.

Order dictionary::

    {"id": "ord-1", "domains": [...], "status": "pending"|"ready"|"valid"|"invalid",
     "authorizations": {domain: "pending"|"valid"|"invalid"},
     "challenges": {challenge_id: {"id", "domain", "status", "created"}},
     "account_key_id": "key-1", "chain": [str]}

``AcmeError.reason`` values: "domain_revoked", "order_not_valid".
``ValueError`` is raised for an empty domain list.
"""

import copy
import threading


class AcmeError(Exception):
    """ACME protocol failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AcmeOrderRenewal:
    """Drive ACME orders from pending to valid and guard renewals."""

    def __init__(
        self,
        account_key_id="key-1",
        intermediates=("intermediate-ca",),
        allowed_zones=("example.com",),
        renewal_days=30,
        challenge_timeout=300,
        max_cname_hops=5,
    ):
        self.account_key_id = account_key_id
        self.intermediates = tuple(intermediates)
        self.allowed_zones = tuple(allowed_zones)
        self.renewal_days = renewal_days
        self.challenge_timeout = challenge_timeout
        self.max_cname_hops = max_cname_hops
        self._orders = {}
        self._next_order = 1
        self._nonces = set()
        self._next_nonce = 1
        self._locks = {}
        self._certs = {}
        self._revoked = set()
        self._nonce_lock = threading.Lock()
        self._lock_guard = threading.Lock()
        self._cert_guard = threading.Lock()

    @staticmethod
    def _canonical_domain(domain):
        return domain.strip().lower().rstrip(".")

    def _is_allowed_domain(self, domain):
        name = self._canonical_domain(domain)
        if not name:
            return False
        for zone in self.allowed_zones:
            zone_name = self._canonical_domain(zone)
            if name == zone_name or name.endswith("." + zone_name):
                return True
        return False

    @staticmethod
    def _refresh_order_status(order):
        states = list(order["authorizations"].values())
        if any(state == "invalid" for state in states):
            order["status"] = "invalid"
        elif states and all(state == "valid" for state in states):
            order["status"] = "ready"
        else:
            order["status"] = "pending"

    def start_order(self, domains, account_key_id=None, now=0):
        """Create a pending order for *domains* and return a copy.

        Empty lists raise ``ValueError("empty_domains")``; a revoked domain
        raises ``AcmeError("domain_revoked")``.
        """
        domains = list(domains)
        if not domains:
            raise ValueError("empty_domains")

        for domain in domains:
            if self._canonical_domain(domain) in self._revoked:
                raise AcmeError("domain_revoked")

        order_id = "ord-%d" % self._next_order
        self._next_order += 1
        order = {
            "id": order_id,
            "domains": domains,
            "status": "pending",
            "authorizations": {domain: "pending" for domain in domains},
            "challenges": {},
            "account_key_id": (
                self.account_key_id if account_key_id is None else account_key_id
            ),
            "chain": [],
        }
        self._orders[order_id] = order
        return copy.deepcopy(order)

    def get_order(self, order_id):
        """Return an order copy or raise ``KeyError``."""
        return copy.deepcopy(self._orders[order_id])

    def add_challenge(self, order_id, domain, challenge_id, now=0):
        """Add a pending challenge for one domain of an order."""
        order = self._orders[order_id]
        if domain not in order["authorizations"]:
            raise KeyError(domain)
        challenge = {
            "id": challenge_id,
            "domain": domain,
            "status": "pending",
            "created": now,
        }
        order["challenges"][challenge_id] = challenge
        return copy.deepcopy(challenge)

    def respond_challenge(self, order_id, challenge_id, valid=True, now=0):
        """Apply a challenge result.

        Returns ``{"status": "valid"|"invalid", "reason": str}``.  A
        challenge older than ``challenge_timeout`` becomes invalid with
        reason "challenge_timeout".  When every authorization is valid the
        order becomes "ready"; an invalid challenge makes the order invalid.
        """
        order = self._orders[order_id]
        challenge = order["challenges"][challenge_id]
        domain = challenge["domain"]

        if order["status"] == "invalid":
            return {"status": "invalid", "reason": "order_invalid"}

        if now - challenge["created"] > self.challenge_timeout:
            challenge["status"] = "invalid"
            order["authorizations"][domain] = "invalid"
            self._refresh_order_status(order)
            return {"status": "invalid", "reason": "challenge_timeout"}

        if not valid:
            challenge["status"] = "invalid"
            order["authorizations"][domain] = "invalid"
            self._refresh_order_status(order)
            return {"status": "invalid", "reason": "challenge_failed"}

        challenge["status"] = "valid"
        order["authorizations"][domain] = "valid"
        if order["status"] != "valid":
            self._refresh_order_status(order)
        return {"status": "valid", "reason": "challenge_valid"}

    def set_authorization(self, order_id, domain, status):
        """Set one authorization state and recompute the order status."""
        if status not in ("pending", "valid", "invalid"):
            raise ValueError("invalid_authorization_status")
        order = self._orders[order_id]
        if domain not in order["authorizations"]:
            raise KeyError(domain)
        order["authorizations"][domain] = status
        if order["status"] != "valid":
            self._refresh_order_status(order)
        return copy.deepcopy(order)

    def finalize(self, order_id, account_key_id=None):
        """Finalize a ready order.

        Pending orders return ``{"status": "pending", "error":
        "authorizations_incomplete"}``; invalid orders return status/error
        "invalid"/"invalid_authorization"; a different account key returns
        error "account_key_mismatch" without changing the order.  Success
        stores ``[f"leaf-{order_id}"] + intermediates`` and returns status
        "valid".  Finalizing a valid order returns status "valid" with
        ``duplicate`` true and the same chain.
        """
        order = self._orders[order_id]
        signing_key = self.account_key_id if account_key_id is None else account_key_id
        if signing_key != order["account_key_id"]:
            return {
                "status": "invalid",
                "error": "account_key_mismatch",
            }

        if order["status"] == "valid":
            return {
                "status": "valid",
                "duplicate": True,
                "chain": list(order["chain"]),
            }

        if order["status"] == "invalid":
            return {
                "status": "invalid",
                "error": "invalid_authorization",
            }

        if order["status"] != "ready":
            return {
                "status": "pending",
                "error": "authorizations_incomplete",
            }

        order["chain"] = ["leaf-" + order_id] + list(self.intermediates)
        order["status"] = "valid"
        return {
            "status": "valid",
            "duplicate": False,
            "chain": list(order["chain"]),
        }

    def download(self, order_id):
        """Return ``{"chain": [...]}`` for a valid order, else raise
        ``AcmeError("order_not_valid")``.  The chain is leaf first.
        """
        order = self._orders[order_id]
        if order["status"] != "valid" or not order["chain"]:
            raise AcmeError("order_not_valid")
        return {"chain": list(order["chain"])}

    def nonce(self):
        """Return a fresh single-use nonce string."""
        with self._nonce_lock:
            value = "nonce-%d" % self._next_nonce
            self._next_nonce += 1
            self._nonces.add(value)
        return value

    def consume_nonce(self, value):
        """Consume *value* once.

        Returns ``{"ok": True, "consumed": True}`` or, for an unknown or
        already used nonce, ``{"ok": False, "reason": "bad_nonce",
        "retry_after": 1}``.
        """
        with self._nonce_lock:
            if value in self._nonces:
                self._nonces.remove(value)
                return {"ok": True, "consumed": True}
        return {"ok": False, "reason": "bad_nonce", "retry_after": 1}

    def authorize_dns(self, domain, records):
        """Follow a CNAME chain hop by hop.

        Every hop target must stay inside ``allowed_zones`` (equal to a zone
        or a subdomain of it), the chain may not loop, and it may not exceed
        ``max_cname_hops``.  Returns a bool.
        """
        current = self._canonical_domain(domain)
        if not self._is_allowed_domain(current):
            return False

        normalized_records = {
            self._canonical_domain(source): target
            for source, target in records.items()
        }
        seen = {current}
        hops = 0
        while current in normalized_records:
            target = normalized_records[current]
            if not isinstance(target, str):
                return False
            target = self._canonical_domain(target)
            hops += 1
            if hops > self.max_cname_hops:
                return False
            if target in seen or not self._is_allowed_domain(target):
                return False
            seen.add(target)
            current = target
        return True

    def renewal_due(self, not_after, now):
        """Return True when ``not_after - now`` is at most renewal_days."""
        return not_after - now <= self.renewal_days * 86400

    def acquire_renewal_lock(self, cert_id, owner):
        """Return True when *owner* holds the single-flight renewal lock."""
        with self._lock_guard:
            current = self._locks.get(cert_id)
            if current is None:
                self._locks[cert_id] = owner
                return True
            return current == owner

    def release_renewal_lock(self, cert_id, owner):
        """Release the lock only when *owner* holds it."""
        with self._lock_guard:
            if self._locks.get(cert_id) != owner:
                return False
            del self._locks[cert_id]
            return True

    def store_certificate(self, cert_id, chain, issued_at, not_after):
        """Store a certificate unless it is older than the current one.

        Returns ``{"stored": False, "reason": "stale_certificate"}`` when
        ``issued_at`` is not newer than the stored certificate.
        """
        with self._cert_guard:
            current = self._certs.get(cert_id)
            if current is not None and issued_at <= current["issued_at"]:
                return {"stored": False, "reason": "stale_certificate"}
            self._certs[cert_id] = {
                "chain": list(chain),
                "issued_at": issued_at,
                "not_after": not_after,
            }
            return {"stored": True}

    def current_certificate(self, cert_id):
        """Return a copy of the stored certificate, or None."""
        certificate = self._certs.get(cert_id)
        return copy.deepcopy(certificate)

    def revoke(self, domain):
        """Blacklist *domain* so future orders are refused."""
        self._revoked.add(self._canonical_domain(domain))
