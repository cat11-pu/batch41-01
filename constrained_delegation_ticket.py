"""Constrained delegation ticket arbitration.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the delegation policy is implemented.

Ticket dictionary accepted by :meth:`DelegationTicketArbiter.authorize`::

    {
        "user": "alice",
        "service": "svc-a",
        "flags": ["forwardable"],          # comma separated string is accepted too
        "pac_groups": ["group-a"],
        "pac_signature": "<hex digest>",  # created by sign_pac()
        "transit": [],                     # services already traversed
        "origin": "svc-front",             # service asking for the delegation
        "account_delegatable": True,
        "realm": "corp.local",
        "referral": False,
        "proxy": False,
        "version": 1,
    }

Decision dictionary returned by :meth:`authorize`::

    {"allowed": bool, "reason": str, "chain": [str], "origin": str,
     "user": str, "service": str, "version": int}

Reason codes: "ok", "missing_forwardable", "proxy_not_allowed",
"service_not_allowed", "pac_signature_invalid", "account_not_delegatable",
"untrusted_realm", "referral_required", "transit_not_allowed",
"transit_loop", "depth_exceeded".
"""

import hashlib
import hmac


class DelegationTicketArbiter:
    """Decide whether a ticket may be used for constrained delegation."""

    KNOWN_FLAGS = frozenset({"forwardable", "forwarded", "proxy", "constrained"})

    def __init__(
        self,
        allowed_services,
        pac_key="unit-test-pac-key",
        max_depth=1,
        trusted_realms=(),
        home_realm="corp.local",
    ):
        self.allowed_services = tuple(allowed_services)
        self.pac_key = pac_key
        self.max_depth = max_depth
        self.trusted_realms = tuple(trusted_realms)
        self.home_realm = home_realm
        self._cache = {}
        self._audit = []

    def sign_pac(self, groups):
        """Return the hex HMAC-SHA256 of the newline joined *groups*.

        The digest is keyed with ``pac_key``; trimming a group from the PAC
        therefore invalidates the signature.
        """
        raise NotImplementedError

    def verify_pac(self, groups, signature):
        """Return True when *signature* is the digest of *groups*."""
        raise NotImplementedError

    def parse_flags(self, raw):
        """Return a frozenset of lower-case flags from a string or iterable.

        Empty items are ignored.  ``ValueError`` is raised for a flag that is
        not listed in :attr:`KNOWN_FLAGS`.
        """
        raise NotImplementedError

    def authorize(self, ticket, now=0):
        """Return an allow/deny decision for *ticket* and append an audit row.

        Checks, in this order: the forwardable flag (plus the proxy flag when
        ``ticket["proxy"]`` is true), exact service whitelist membership,
        PAC signature, account delegatability, realm/referral trust, transit
        path (all services known, no repeated hop), and finally
        ``len(transit) < max_depth``.  The returned chain is ``transit`` plus
        the target service.
        """
        raise NotImplementedError

    def cache_decision(self, user, service, decision, version=1):
        """Store *decision* under the exact ``(user, service, version)`` key."""
        raise NotImplementedError

    def lookup_decision(self, user, service, version=1):
        """Return the cached decision for the exact key, else None."""
        raise NotImplementedError

    def audit(self):
        """Return a list of decision dictionaries in call order."""
        raise NotImplementedError
