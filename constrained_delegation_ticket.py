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
import threading


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
        self._lock = threading.RLock()

    def sign_pac(self, groups):
        """Return the hex HMAC-SHA256 of the newline joined *groups*.

        The digest is keyed with ``pac_key``; trimming a group from the PAC
        therefore invalidates the signature.
        """
        payload = "\n".join(groups).encode("utf-8")
        return hmac.new(
            self.pac_key.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()

    def verify_pac(self, groups, signature):
        """Return True when *signature* is the digest of *groups*."""
        if not isinstance(signature, str):
            return False
        return hmac.compare_digest(self.sign_pac(groups), signature)

    def parse_flags(self, raw):
        """Return a frozenset of lower-case flags from a string or iterable.

        Empty items are ignored.  ``ValueError`` is raised for a flag that is
        not listed in :attr:`KNOWN_FLAGS`.
        """
        if isinstance(raw, str):
            items = raw.split(",")
        else:
            items = raw
        flags = set()
        for item in items:
            flag = str(item).strip().lower()
            if not flag:
                continue
            if flag not in self.KNOWN_FLAGS:
                raise ValueError("unknown delegation flag: {0}".format(flag))
            flags.add(flag)
        return frozenset(flags)

    def authorize(self, ticket, now=0):
        """Return an allow/deny decision for *ticket* and append an audit row.

        Checks, in this order: the forwardable flag (plus the proxy flag when
        ``ticket["proxy"]`` is true), exact service whitelist membership,
        PAC signature, account delegatability, realm/referral trust, transit
        path (all services known, no repeated hop), and finally
        ``len(transit) < max_depth``.  The returned chain is ``transit`` plus
        the target service.
        """
        service = ticket["service"]
        transit = list(ticket.get("transit") or ())
        chain = transit + [service]

        def deny(reason):
            return self._record(
                ticket,
                {
                    "allowed": False,
                    "reason": reason,
                    "chain": chain,
                    "origin": ticket.get("origin"),
                    "user": ticket["user"],
                    "service": service,
                    "version": ticket.get("version"),
                },
            )

        flags = self.parse_flags(ticket.get("flags") or ())
        if "forwardable" not in flags:
            return deny("missing_forwardable")
        if ticket.get("proxy") and "proxy" not in flags:
            return deny("proxy_not_allowed")

        if service not in self.allowed_services:
            return deny("service_not_allowed")

        if not self.verify_pac(
            ticket.get("pac_groups") or (), ticket.get("pac_signature")
        ):
            return deny("pac_signature_invalid")

        if not ticket.get("account_delegatable"):
            return deny("account_not_delegatable")

        realm = ticket.get("realm")
        if realm != self.home_realm:
            if realm not in self.trusted_realms:
                return deny("untrusted_realm")
            if not ticket.get("referral"):
                return deny("referral_required")

        seen = set()
        for hop in transit:
            if hop not in self.allowed_services:
                return deny("transit_not_allowed")
            if hop in seen:
                return deny("transit_loop")
            seen.add(hop)

        if len(transit) >= self.max_depth:
            return deny("depth_exceeded")

        return self._record(
            ticket,
            {
                "allowed": True,
                "reason": "ok",
                "chain": chain,
                "origin": ticket.get("origin"),
                "user": ticket["user"],
                "service": service,
                "version": ticket.get("version"),
            },
        )

    def cache_decision(self, user, service, decision, version=1):
        """Store *decision* under the exact ``(user, service, version)`` key."""
        with self._lock:
            self._cache[(user, service, version)] = decision

    def lookup_decision(self, user, service, version=1):
        """Return the cached decision for the exact key, else None."""
        with self._lock:
            return self._cache.get((user, service, version))

    def audit(self):
        """Return a list of decision dictionaries in call order."""
        with self._lock:
            return list(self._audit)

    def _record(self, ticket, decision):
        with self._lock:
            entry = dict(decision)
            entry["chain"] = list(decision["chain"])
            self._audit.append(entry)
        return decision
