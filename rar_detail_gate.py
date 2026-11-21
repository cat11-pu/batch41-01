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

import copy
import json
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_EVEN


TWO_PLACES = Decimal("0.01")

PAYMENT_FIELDS = frozenset(
    (
        "type",
        "amount",
        "max_amount",
        "currency",
        "creditor",
        "description",
        "expires_at",
        "metadata",
    )
)
CREDITOR_FIELDS = frozenset(("account", "name"))
REQUIRED_SCOPE = {"payment": "pay"}


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

    def _parse_amount(self, value):
        """Parse an amount into a non-negative Decimal quantized to 2 places."""
        if isinstance(value, bool) or not isinstance(
            value, (int, float, str, Decimal)
        ):
            raise DetailError("invalid_amount")
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise DetailError("invalid_amount")
        if not amount.is_finite() or amount < 0:
            raise DetailError("invalid_amount")
        return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_EVEN)

    def _normalize_creditor(self, creditor):
        if creditor is None:
            raise DetailError("creditor_missing")
        if isinstance(creditor, dict):
            entries = [creditor]
        elif isinstance(creditor, list) and creditor:
            entries = creditor
        elif isinstance(creditor, list):
            raise DetailError("creditor_missing")
        else:
            raise DetailError("invalid_creditor")

        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise DetailError("invalid_creditor")
            if set(entry) - CREDITOR_FIELDS:
                raise DetailError("invalid_creditor")
            account = entry.get("account")
            if not isinstance(account, str) or not account:
                raise DetailError("invalid_creditor")
            if account not in self.allowed_creditors:
                raise DetailError("creditor_not_allowed")
            normalized.append({"account": account, "name": entry.get("name")})

        normalized.sort(key=lambda item: item["account"])
        return normalized

    def _assert_metadata_depth(self, value, level=1):
        if isinstance(value, dict):
            if level > self.max_depth:
                raise DetailError("too_deep")
            for child in value.values():
                self._assert_metadata_depth(child, level + 1)
        elif isinstance(value, list):
            for child in value:
                self._assert_metadata_depth(child, level)

    def _normalize_payment(self, detail):
        if set(detail) - PAYMENT_FIELDS:
            raise DetailError("unknown_field")

        if "currency" not in detail or detail["currency"] is None:
            raise DetailError("currency_missing")
        currency = detail["currency"]
        if currency not in self.allowed_currencies:
            raise DetailError("currency_not_allowed")

        if "amount" not in detail:
            raise DetailError("invalid_amount")
        amounts = [self._parse_amount(detail["amount"])]
        if "max_amount" in detail:
            amounts.append(self._parse_amount(detail["max_amount"]))
        amount = min(amounts)

        creditors = self._normalize_creditor(detail.get("creditor"))

        metadata = None
        if "metadata" in detail and detail["metadata"] is not None:
            metadata = detail["metadata"]
            self._assert_metadata_depth(metadata)
            metadata = copy.deepcopy(metadata)

        return {
            "type": "payment",
            "amount": str(amount),
            "currency": currency,
            "creditor": creditors,
            "description": detail.get("description"),
            "expires_at": detail.get("expires_at"),
            "metadata": metadata,
        }

    def _normalize_one(self, detail):
        if not isinstance(detail, dict):
            raise DetailError("unknown_type")
        detail_type = detail.get("type")
        if detail_type not in REQUIRED_SCOPE:
            raise DetailError("unknown_type")
        if detail_type == "payment":
            return self._normalize_payment(detail)
        raise DetailError("unknown_type")

    def normalize(self, details):
        """Return the canonical, deduplicated list of detail dicts.

        Raises :class:`DetailError` for schema problems.  Identical canonical
        entries collapse into one.
        """
        if not isinstance(details, list) or not details:
            raise DetailError("empty_details")
        if len(details) > self.max_items:
            raise DetailError("too_many_items")

        deduped = []
        seen = set()
        for detail in details:
            canonical = self._normalize_one(detail)
            fingerprint = json.dumps(canonical, sort_keys=True, default=str)
            if fingerprint not in seen:
                seen.add(fingerprint)
                deduped.append(canonical)
        return deduped

    def validate(self, details, scopes=(), now=0):
        """Validate *details* against *scopes* and return the decision dict.

        "payment" requires the "pay" scope.  Expired entries and every other
        schema violation raise :class:`DetailError`.
        """
        canonical = self.normalize(details)
        scopes = tuple(scopes)

        for detail in canonical:
            required_scope = REQUIRED_SCOPE[detail["type"]]
            if required_scope not in scopes:
                raise DetailError("scope_missing")
            expires_at = detail["expires_at"]
            if expires_at is not None and expires_at <= now:
                raise DetailError("detail_expired")

        total = Decimal("0")
        for detail in canonical:
            total += Decimal(detail["amount"])
        total = total.quantize(TWO_PLACES, rounding=ROUND_HALF_EVEN)

        return {
            "allowed": True,
            "reason": "ok",
            "details": canonical,
            "total_amount": str(total),
        }

    def issue_token(self, details, scopes=(), now=0):
        """Return ``{"details": canonical, "issued_at": now,
        "total_amount": str}`` or raise :class:`DetailError`.
        """
        decision = self.validate(details, scopes=scopes, now=now)
        return {
            "details": decision["details"],
            "issued_at": now,
            "total_amount": decision["total_amount"],
        }

    def echo_matches(self, requested, echoed):
        """Return True when *echoed* is the canonical form of *requested*."""
        return self.normalize(requested) == echoed
