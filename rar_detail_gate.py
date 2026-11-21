"""Rich authorization detail (authorization_details) gate.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the schema validation is implemented.

Supported detail types:

``payment``
    keys: type, amount, max_amount, currency, creditor, description,
    expires_at, metadata.  Both amount fields may appear; the stricter
    (smaller) value becomes the canonical amount.  ``creditor`` is either a
    dict or a list of dicts with keys account/name; every account must be in
    the gate whitelist.  ``metadata`` may contain nested mappings.

Canonical detail dict: type, amount (decimal string with 2 places),
currency, creditor (list sorted by account, each with account/name),
description, expires_at, metadata.

``validate`` returns ``{"allowed": True, "reason": "ok", "details": [...],
"total_amount": "<2dp string>"}``.  Any invalid input raises ``DetailError``
whose ``reason`` is one of: "empty_details", "too_many_items", "too_deep",
"unknown_type", "unknown_field", "currency_missing",
"currency_not_allowed", "invalid_amount", "creditor_missing",
"invalid_creditor", "creditor_not_allowed", "detail_expired",
"scope_missing".
"""

import json
from decimal import Decimal


class DetailError(Exception):
    """Validation failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AuthorizationDetailGate:
    """Parse, constrain, and echo authorization_details."""

    def __init__(
        self,
        allowed_currencies=("CNY", "USD", "EUR"),
        allowed_creditors=("acct-1", "acct-2"),
        max_items=100,
        max_depth=3,
    ):
        self.allowed_currencies = tuple(allowed_currencies)
        self.allowed_creditors = tuple(allowed_creditors)
        self.max_items = max_items
        self.max_depth = max_depth

    def normalize(self, details):
        """Return the canonical, deduplicated list of detail dicts.

        Raises :class:`DetailError` for schema problems.  Identical canonical
        entries collapse into one.
        """
        raise NotImplementedError

    def validate(self, details, scopes=(), now=0):
        """Validate *details* against *scopes* and return the decision dict.

        "payment" requires the "pay" scope.  Expired entries and every other
        schema violation raise :class:`DetailError`.
        """
        raise NotImplementedError

    def issue_token(self, details, scopes=(), now=0):
        """Return ``{"details": canonical, "issued_at": now,
        "total_amount": str}`` or raise :class:`DetailError`.
        """
        raise NotImplementedError

    def echo_matches(self, requested, echoed):
        """Return True when *echoed* is the canonical form of *requested*."""
        raise NotImplementedError
