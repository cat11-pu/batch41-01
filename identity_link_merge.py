"""Identity link merging across multiple identity providers.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the merge policy is implemented.

An account key is the tuple ``(issuer, sub)``.  Account records are dicts
with ``issuer``, ``sub``, ``email`` (lower-case), ``email_verified``,
``tenant``, ``groups`` (list), and ``key``.  ``email`` is only a hint, never
the primary key.
"""


class MergeFailed(Exception):
    """Raised after an injected commit failure has been rolled back."""


class IdentityLinkMerger:
    """Merge identity accounts transactionally with a replayable log."""

    def __init__(self, default_tenant="default"):
        self.default_tenant = default_tenant
        self._accounts = {}
        self._sessions = {}
        self._tokens = {}
        self._history = []
        self._merges = {}
        self._fault = None

    def register(self, issuer, sub, email, email_verified=False, tenant="default", groups=()):
        """Register an account and return a copy of its record.

        Empty issuer or sub raises ``ValueError`` (message contains
        "issuer" or "sub").  The email is stripped and lower-cased.
        """
        raise NotImplementedError

    def get(self, key):
        """Return an account record copy for an account key, or None."""
        raise NotImplementedError

    def accounts(self):
        """Return copies of all active account records."""
        raise NotImplementedError

    def resolve(self, issuer, sub, email, email_verified=False, tenant="default"):
        """Resolve a login to an account record.

        ``(issuer, sub)`` wins.  When the sub is unknown but a verified
        account in the same tenant and issuer owns the same normalized email,
        that account is reused (the sub may have changed on re-registration).
        Otherwise a new account is registered.
        """
        raise NotImplementedError

    def plan_merge(self, primary_key, secondary_key, force=False):
        """Return ``{"status": "ok"}`` or a conflict description.

        Conflict reasons: "tenant_mismatch", "email_unverified" (secondary),
        "email_collision" (same normalized email on different issuers).
        A conflict carries ``"candidates"`` with both keys.
        """
        raise NotImplementedError

    def merge(self, primary_key, secondary_key, force=False, confirmation=None):
        """Merge *secondary_key* into *primary_key*.

        Repeat calls return ``{"status": "already_merged"}`` with the original
        merge id.  ``force=True`` requires ``confirmation == "CONFIRM"``.
        Success moves group membership, invalidates the secondary sessions
        and refresh tokens, archives the secondary account, appends one
        replayable history row, and returns
        ``{"status": "merged", "merge_id": str, "groups_moved": int,
        "primary": key, "secondary": key}``.
        """
        raise NotImplementedError

    def unmerge(self, merge_id):
        """Restore both accounts from the merge snapshot.

        Returns ``{"status": "unmerged", "merge_id": merge_id}``.
        """
        raise NotImplementedError

    def history(self):
        """Return replayable merge rows.

        Each row has merge_id, primary, secondary, email, groups_moved, and
        ``actions`` containing "invalidate_sessions", "invalidate_tokens",
        and "move_groups".
        """
        raise NotImplementedError

    def snapshot(self):
        """Return a detached dict of account-key -> account record."""
        raise NotImplementedError

    def add_session(self, key, token):
        """Bind a login session token to an account key."""
        raise NotImplementedError

    def session_valid(self, key, token):
        """Return True only for a live session of that account."""
        raise NotImplementedError

    def add_refresh_token(self, key, token):
        """Register a refresh token for an account key."""
        raise NotImplementedError

    def exchange_refresh(self, key, token):
        """Return True when the refresh token may still be exchanged."""
        raise NotImplementedError

    def inject_fault(self, stage="commit"):
        """Arrange the next merge to fail at the named stage."""
        raise NotImplementedError
