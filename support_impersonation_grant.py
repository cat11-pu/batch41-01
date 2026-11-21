"""Impersonation grants for support agents.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the grant policy is implemented.

A grant is bound to one ticket and one target user.  Only the "read" scope
is allowed.  Activation needs two distinct approvers.  Tokens use the
"impersonation" audience and never the normal "user" audience.
"""


class GrantError(Exception):
    """Grant policy failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ImpersonationGrantBook:
    """Book impersonation requests, approvals, tokens, and revocation."""

    def __init__(self, tenant="default", max_concurrent=3, max_ttl=3600):
        self.tenant = tenant
        self.max_concurrent = max_concurrent
        self.max_ttl = max_ttl
        self._tickets = {}
        self._grants = {}
        self._next_grant = 1
        self._tokens = {}
        self._notifications = []
        self._audit = []

    def open_ticket(self, ticket_id, tenant=None):
        """Open a ticket, defaulting to this book's tenant, and return a copy.

        ``request_grant`` rejects a ticket whose tenant differs from the
        book's tenant.
        """
        raise NotImplementedError

    def close_ticket(self, ticket_id):
        """Close a ticket and revoke every grant bound to it."""
        raise NotImplementedError

    def reassign_ticket(self, ticket_id, assignee):
        """Reassign a ticket and revoke every grant bound to it."""
        raise NotImplementedError

    def request_grant(self, ticket_id, agent, target, scopes=("read",), ttl=600, now=0):
        """Create a pending grant and return a copy with a grant_id.

        Raises ``GrantError`` with reason "empty_ticket", "ticket_closed",
        "tenant_mismatch", "write_not_allowed", "invalid_ttl", or
        "concurrency_limit" (pending plus active grants per agent, three by
        default).
        """
        raise NotImplementedError

    def approve(self, grant_id, approver, now=0):
        """Record one approval and return the grant copy.

        Two distinct approvers activate the grant.  A repeated approver
        raises ``GrantError("duplicate_approver")``.
        """
        raise NotImplementedError

    def get_grant(self, grant_id):
        """Return a grant copy or raise ``KeyError``."""
        raise NotImplementedError

    def check_access(self, grant_id, scope="read", now=0):
        """Return True for read access while the grant is active.

        Expired grants become "expired"; grants whose ticket was closed or
        reassigned become "revoked".  A non-read scope returns False.
        """
        raise NotImplementedError

    def issue_token(self, grant_id, now=0):
        """Return an impersonation token or raise ``GrantError("not_active")``.

        Token keys: grant_id, aud ("impersonation"), sub (target), agent,
        expires_at.
        """
        raise NotImplementedError

    def check_token(self, token, scope="read", now=0):
        """Return True only for a live token with the impersonation audience."""
        raise NotImplementedError

    def revoke(self, grant_id, now=0):
        """Revoke a grant, its tokens, and notify the target user."""
        raise NotImplementedError

    def notifications(self, user):
        """Return notification copies addressed to *user*.

        Activation adds a row with kind "impersonation_started" for both the
        target and the agent.
        """
        raise NotImplementedError

    def audit(self):
        """Return audit rows with action, grant_id, agent, target, ticket_id.

        Activation rows use action "activated".
        """
        raise NotImplementedError
