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

    def start_order(self, domains, account_key_id=None, now=0):
        """Create a pending order for *domains* and return a copy.

        Empty lists raise ``ValueError("empty_domains")``; a revoked domain
        raises ``AcmeError("domain_revoked")``.
        """
        raise NotImplementedError

    def get_order(self, order_id):
        """Return an order copy or raise ``KeyError``."""
        raise NotImplementedError

    def add_challenge(self, order_id, domain, challenge_id, now=0):
        """Add a pending challenge for one domain of an order."""
        raise NotImplementedError

    def respond_challenge(self, order_id, challenge_id, valid=True, now=0):
        """Apply a challenge result.

        Returns ``{"status": "valid"|"invalid", "reason": str}``.  A
        challenge older than ``challenge_timeout`` becomes invalid with
        reason "challenge_timeout".  When every authorization is valid the
        order becomes "ready"; an invalid challenge makes the order invalid.
        """
        raise NotImplementedError

    def set_authorization(self, order_id, domain, status):
        """Set one authorization state and recompute the order status."""
        raise NotImplementedError

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
        raise NotImplementedError

    def download(self, order_id):
        """Return ``{"chain": [...]}`` for a valid order, else raise
        ``AcmeError("order_not_valid")``.  The chain is leaf first.
        """
        raise NotImplementedError

    def nonce(self):
        """Return a fresh single-use nonce string."""
        raise NotImplementedError

    def consume_nonce(self, value):
        """Consume *value* once.

        Returns ``{"ok": True, "consumed": True}`` or, for an unknown or
        already used nonce, ``{"ok": False, "reason": "bad_nonce",
        "retry_after": 1}``.
        """
        raise NotImplementedError

    def authorize_dns(self, domain, records):
        """Follow a CNAME chain hop by hop.

        Every hop target must stay inside ``allowed_zones`` (equal to a zone
        or a subdomain of it), the chain may not loop, and it may not exceed
        ``max_cname_hops``.  Returns a bool.
        """
        raise NotImplementedError

    def renewal_due(self, not_after, now):
        """Return True when ``not_after - now`` is at most renewal_days."""
        raise NotImplementedError

    def acquire_renewal_lock(self, cert_id, owner):
        """Return True when *owner* holds the single-flight renewal lock."""
        raise NotImplementedError

    def release_renewal_lock(self, cert_id, owner):
        """Release the lock only when *owner* holds it."""
        raise NotImplementedError

    def store_certificate(self, cert_id, chain, issued_at, not_after):
        """Store a certificate unless it is older than the current one.

        Returns ``{"stored": False, "reason": "stale_certificate"}`` when
        ``issued_at`` is not newer than the stored certificate.
        """
        raise NotImplementedError

    def current_certificate(self, cert_id):
        """Return a copy of the stored certificate, or None."""
        raise NotImplementedError

    def revoke(self, domain):
        """Blacklist *domain* so future orders are refused."""
        raise NotImplementedError
