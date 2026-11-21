"""Hold contact (email/phone) changes behind notifications and confirmation.

A change request first notifies the old address, then sends two single-use
tokens to the new address.  Both tokens must be confirmed from the same
session.  Completion updates the contact, invalidates old sessions and
recovery codes, and starts the per-account cooldown.  Admin changes follow
the same cooldown and normalization rules.
"""


class ChangeFailed(Exception):
    """Raised after an injected commit failure has been rolled back."""


class ContactChangeHold:
    """Coordinate contact changes with notifications and cooldown."""

    def __init__(self, cooldown=86400, token_ttl=900):
        self.cooldown = cooldown
        self.token_ttl = token_ttl
        self._accounts = {}
        self._changes = {}
        self._tokens = {}
        self._notifications = []
        self._audit = []
        self._next_change = 1
        self._fault = None

    @staticmethod
    def _normalize(contact):
        return (contact or "").strip().lower()

    def _account(self, account):
        if account not in self._accounts:
            raise KeyError(account)
        return self._accounts[account]

    def _record(self, action, **fields):
        row = {"action": action}
        row.update(fields)
        self._audit.append(row)

    def _notify(self, address, kind, change_id):
        self._notifications.append(
            {"address": address, "kind": kind, "change_id": change_id}
        )

    def _check_cooldown(self, acct, now):
        last = acct["last_change"]
        if last is not None and now - last < self.cooldown:
            raise ValueError("cooldown_active")

    def register_account(self, account, contact, now=0):
        """Create an account with a normalized contact address."""
        normalized = self._normalize(contact)
        if not normalized:
            raise ValueError("empty_contact")
        self._accounts[account] = {
            "contact": normalized,
            "sessions": set(),
            "recovery": set(),
            "last_change": None,
            "pending": None,
        }

    def contact(self, account):
        """Return the normalized current contact or raise KeyError."""
        return self._account(account)["contact"]

    def add_session(self, account, session_id):
        """Add a live session for the account."""
        self._account(account)["sessions"].add(session_id)

    def session_valid(self, account, session_id):
        """Return True when the session is still live."""
        return session_id in self._account(account)["sessions"]

    def add_recovery_code(self, account, code):
        """Add a recovery code for the account."""
        self._account(account)["recovery"].add(code)

    def recovery_valid(self, account, code):
        """Return True when the recovery code is still live."""
        return code in self._account(account)["recovery"]

    def request_change(self, account, new_contact, session_id, now=0):
        """Start a change and return ``{"change_id", "tokens", "status"}``.

        ``tokens`` holds two single-use tokens.  Invalid input raises
        ValueError with reason "empty_contact", "contact_unchanged",
        "change_pending", or "cooldown_active".  The cooldown blocks a new
        change while ``now - last_change`` is strictly less than
        ``cooldown``.
        """
        acct = self._account(account)
        normalized = self._normalize(new_contact)
        if not normalized:
            raise ValueError("empty_contact")
        if normalized == acct["contact"]:
            raise ValueError("contact_unchanged")
        if acct["pending"] is not None:
            raise ValueError("change_pending")
        self._check_cooldown(acct, now)

        change_id = self._next_change
        self._next_change += 1
        tokens = ["tok-%d-a" % change_id, "tok-%d-b" % change_id]
        change = {
            "id": change_id,
            "account": account,
            "old_contact": acct["contact"],
            "new_contact": normalized,
            "session": session_id,
            "tokens": tokens,
            "used": set(),
            "created": now,
            "status": "pending",
        }
        self._changes[change_id] = change
        for token in tokens:
            self._tokens[token] = change_id
        acct["pending"] = change_id

        self._record("requested", change_id=change_id, account=account, now=now)
        self._notify(change["old_contact"], "change_requested_old", change_id)
        self._record(
            "old_notified", change_id=change_id, account=account, now=now
        )
        for _token in tokens:
            self._notify(normalized, "verify_new", change_id)
        self._record(
            "new_notified", change_id=change_id, account=account, now=now
        )
        return {"change_id": change_id, "tokens": list(tokens), "status": "pending"}

    def _commit(self, change, now):
        acct = self._accounts[change["account"]]
        snapshot = (
            acct["contact"],
            set(acct["sessions"]),
            set(acct["recovery"]),
            acct["last_change"],
        )
        try:
            acct["contact"] = change["new_contact"]
            acct["sessions"] = set()
            acct["recovery"] = set()
            if self._fault == "commit":
                raise RuntimeError("injected commit failure")
            acct["last_change"] = now
            change["status"] = "completed"
        except Exception:
            (
                acct["contact"],
                acct["sessions"],
                acct["recovery"],
                acct["last_change"],
            ) = snapshot
            change["status"] = "failed"
            raise

    def confirm_change(self, change_id, token, session_id, now=0):
        """Confirm one of the two tokens.

        The first valid token returns status "pending_second_confirmation";
        the second completes the change.  Repeating the same token raises
        ValueError("duplicate_confirmation").  A used token offered to a
        different change raises ValueError("token_used").  Other failures:
        "invalid_token", "token_expired", "session_mismatch".  A repeated
        confirmation of an already completed change returns status
        "completed" with ``duplicate`` true.
        """
        if change_id not in self._changes:
            raise ValueError("invalid_change")
        change = self._changes[change_id]
        if token not in self._tokens:
            raise ValueError("invalid_token")
        owner = self._tokens[token]
        if owner != change_id:
            if token in self._changes[owner]["used"]:
                raise ValueError("token_used")
            raise ValueError("invalid_token")
        if change["status"] == "completed":
            return {"status": "completed", "duplicate": True}
        if token in change["used"]:
            raise ValueError("duplicate_confirmation")
        if now - change["created"] > self.token_ttl:
            raise ValueError("token_expired")
        if session_id != change["session"]:
            raise ValueError("session_mismatch")

        change["used"].add(token)
        self._record(
            "confirmation",
            change_id=change_id,
            account=change["account"],
            now=now,
        )
        if change["used"] != set(change["tokens"]):
            return {"status": "pending_second_confirmation"}

        try:
            self._commit(change, now)
        except Exception:
            self._fault = None
            raise ChangeFailed("commit failed; change rolled back")
        self._accounts[change["account"]]["pending"] = None
        self._record(
            "completed",
            change_id=change_id,
            account=change["account"],
            now=now,
        )
        return {"status": "completed"}

    def admin_change(self, account, new_contact, admin, now=0):
        """Apply an admin change immediately, respecting cooldown.

        Sessions and recovery codes are invalidated.  The audit row has
        action "admin_changed" and its ``actor`` is *admin*.
        """
        acct = self._account(account)
        normalized = self._normalize(new_contact)
        if not normalized:
            raise ValueError("empty_contact")
        self._check_cooldown(acct, now)
        acct["contact"] = normalized
        acct["sessions"] = set()
        acct["recovery"] = set()
        acct["last_change"] = now
        acct["pending"] = None
        self._record("admin_changed", account=account, actor=admin, now=now)
        return {"status": "completed"}

    def change_status(self, change_id):
        """Return the change status string."""
        return self._changes[change_id]["status"]

    def notifications(self, address):
        """Return notification copies delivered to *address*.

        Kinds are "change_requested_old" for the old address and
        "verify_new" for each token sent to the new address.
        """
        normalized = self._normalize(address)
        return [n for n in self._notifications if n["address"] == normalized]

    def audit(self):
        """Return audit rows for every workflow step.

        Actions include "requested", "old_notified", "new_notified",
        "confirmation", "completed", and "admin_changed".
        """
        return list(self._audit)

    def inject_fault(self, stage="commit"):
        """Arrange the next confirmation commit to fail after rollback."""
        self._fault = stage
