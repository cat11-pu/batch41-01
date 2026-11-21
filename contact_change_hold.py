"""Hold contact (email/phone) changes behind notifications and confirmation.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the change workflow is implemented.

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

    def register_account(self, account, contact, now=0):
        """Create an account with a normalized contact address."""
        raise NotImplementedError

    def contact(self, account):
        """Return the normalized current contact or raise KeyError."""
        raise NotImplementedError

    def add_session(self, account, session_id):
        """Add a live session for the account."""
        raise NotImplementedError

    def session_valid(self, account, session_id):
        """Return True when the session is still live."""
        raise NotImplementedError

    def add_recovery_code(self, account, code):
        """Add a recovery code for the account."""
        raise NotImplementedError

    def recovery_valid(self, account, code):
        """Return True when the recovery code is still live."""
        raise NotImplementedError

    def request_change(self, account, new_contact, session_id, now=0):
        """Start a change and return ``{"change_id", "tokens", "status"}``.

        ``tokens`` holds two single-use tokens.  Invalid input raises
        ValueError with reason "empty_contact", "contact_unchanged",
        "change_pending", or "cooldown_active".  The cooldown blocks a new
        change while ``now - last_change`` is strictly less than
        ``cooldown``.
        """
        raise NotImplementedError

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
        raise NotImplementedError

    def admin_change(self, account, new_contact, admin, now=0):
        """Apply an admin change immediately, respecting cooldown.

        Sessions and recovery codes are invalidated.  The audit row has
        action "admin_changed" and its ``actor`` is *admin*.
        """
        raise NotImplementedError

    def change_status(self, change_id):
        """Return the change status string."""
        raise NotImplementedError

    def notifications(self, address):
        """Return notification copies delivered to *address*.

        Kinds are "change_requested_old" for the old address and
        "verify_new" for each token sent to the new address.
        """
        raise NotImplementedError

    def audit(self):
        """Return audit rows for every workflow step.

        Actions include "requested", "old_notified", "new_notified",
        "confirmation", "completed", and "admin_changed".
        """
        raise NotImplementedError

    def inject_fault(self, stage="commit"):
        """Arrange the next confirmation commit to fail after rollback."""
        raise NotImplementedError
