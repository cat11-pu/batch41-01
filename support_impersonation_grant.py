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

    AUDIENCE = "impersonation"
    REQUIRED_APPROVALS = 2

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

    @staticmethod
    def _copy_grant(grant):
        copy = dict(grant)
        copy["scopes"] = list(grant["scopes"])
        copy["approvers"] = list(grant["approvers"])
        return copy

    def _notify(self, user, kind, grant):
        self._notifications.append({
            "user": user,
            "kind": kind,
            "grant_id": grant["grant_id"],
            "ticket_id": grant["ticket_id"],
            "agent": grant["agent"],
            "target": grant["target"],
        })

    def _record_audit(self, action, grant):
        self._audit.append({
            "action": action,
            "grant_id": grant["grant_id"],
            "agent": grant["agent"],
            "target": grant["target"],
            "ticket_id": grant["ticket_id"],
        })

    def _revoke_grant(self, grant):
        if grant["status"] in ("revoked", "expired"):
            return
        grant["status"] = "revoked"
        self._tokens.pop(grant["grant_id"], None)
        self._notify(grant["target"], "impersonation_revoked", grant)
        self._notify(grant["agent"], "impersonation_revoked", grant)
        self._record_audit("revoked", grant)

    def _refresh_status(self, grant, now):
        if grant["status"] == "active" and now >= grant["expires_at"]:
            grant["status"] = "expired"
            self._tokens.pop(grant["grant_id"], None)
            self._record_audit("expired", grant)

    def open_ticket(self, ticket_id, tenant=None):
        """Open a ticket, defaulting to this book's tenant, and return a copy.

        ``request_grant`` rejects a ticket whose tenant differs from the
        book's tenant.
        """
        ticket = {
            "ticket_id": ticket_id,
            "tenant": self.tenant if tenant is None else tenant,
            "status": "open",
            "assignee": None,
        }
        self._tickets[ticket_id] = ticket
        return dict(ticket)

    def close_ticket(self, ticket_id):
        """Close a ticket and revoke every grant bound to it."""
        ticket = self._tickets[ticket_id]
        ticket["status"] = "closed"
        for grant in self._grants.values():
            if grant["ticket_id"] == ticket_id:
                self._revoke_grant(grant)
        return dict(ticket)

    def reassign_ticket(self, ticket_id, assignee):
        """Reassign a ticket and revoke every grant bound to it."""
        ticket = self._tickets[ticket_id]
        ticket["assignee"] = assignee
        for grant in self._grants.values():
            if grant["ticket_id"] == ticket_id:
                self._revoke_grant(grant)
        return dict(ticket)

    def request_grant(self, ticket_id, agent, target, scopes=("read",), ttl=600, now=0):
        """Create a pending grant and return a copy with a grant_id.

        Raises ``GrantError`` with reason "empty_ticket", "ticket_closed",
        "tenant_mismatch", "write_not_allowed", "invalid_ttl", or
        "concurrency_limit" (pending plus active grants per agent, three by
        default).
        """
        if not ticket_id:
            raise GrantError("empty_ticket")
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise GrantError("unknown_ticket")
        if ticket["status"] == "closed":
            raise GrantError("ticket_closed")
        if ticket["tenant"] != self.tenant:
            raise GrantError("tenant_mismatch")
        if any(scope != "read" for scope in scopes):
            raise GrantError("write_not_allowed")
        if not isinstance(ttl, int) or ttl <= 0 or ttl > self.max_ttl:
            raise GrantError("invalid_ttl")
        held = sum(
            1
            for grant in self._grants.values()
            if grant["agent"] == agent
            and grant["status"] in ("pending_approval", "active")
        )
        if held >= self.max_concurrent:
            raise GrantError("concurrency_limit")
        grant = {
            "grant_id": "G-%d" % self._next_grant,
            "ticket_id": ticket_id,
            "agent": agent,
            "target": target,
            "scopes": list(scopes),
            "ttl": ttl,
            "status": "pending_approval",
            "approvers": [],
            "created_at": now,
            "expires_at": None,
        }
        self._next_grant += 1
        self._grants[grant["grant_id"]] = grant
        self._record_audit("requested", grant)
        return self._copy_grant(grant)

    def approve(self, grant_id, approver, now=0):
        """Record one approval and return the grant copy.

        Two distinct approvers activate the grant.  A repeated approver
        raises ``GrantError("duplicate_approver")``.
        """
        grant = self._grants[grant_id]
        if grant["status"] != "pending_approval":
            raise GrantError("not_pending")
        if approver in grant["approvers"]:
            raise GrantError("duplicate_approver")
        grant["approvers"].append(approver)
        if len(grant["approvers"]) >= self.REQUIRED_APPROVALS:
            grant["status"] = "active"
            grant["expires_at"] = now + grant["ttl"]
            self._notify(grant["target"], "impersonation_started", grant)
            self._notify(grant["agent"], "impersonation_started", grant)
            self._record_audit("activated", grant)
        return self._copy_grant(grant)

    def get_grant(self, grant_id):
        """Return a grant copy or raise ``KeyError``."""
        return self._copy_grant(self._grants[grant_id])

    def check_access(self, grant_id, scope="read", now=0):
        """Return True for read access while the grant is active.

        Expired grants become "expired"; grants whose ticket was closed or
        reassigned become "revoked".  A non-read scope returns False.
        """
        if scope != "read":
            return False
        grant = self._grants[grant_id]
        self._refresh_status(grant, now)
        return grant["status"] == "active"

    def issue_token(self, grant_id, now=0):
        """Return an impersonation token or raise ``GrantError("not_active")``.

        Token keys: grant_id, aud ("impersonation"), sub (target), agent,
        expires_at.
        """
        grant = self._grants[grant_id]
        self._refresh_status(grant, now)
        if grant["status"] != "active":
            raise GrantError("not_active")
        token = {
            "grant_id": grant["grant_id"],
            "aud": self.AUDIENCE,
            "sub": grant["target"],
            "agent": grant["agent"],
            "expires_at": grant["expires_at"],
        }
        self._tokens.setdefault(grant["grant_id"], []).append(token)
        return dict(token)

    def check_token(self, token, scope="read", now=0):
        """Return True only for a live token with the impersonation audience."""
        if scope != "read":
            return False
        if not isinstance(token, dict) or token.get("aud") != self.AUDIENCE:
            return False
        grant = self._grants.get(token.get("grant_id"))
        if grant is None:
            return False
        self._refresh_status(grant, now)
        if grant["status"] != "active":
            return False
        issued = self._tokens.get(grant["grant_id"], [])
        return any(dict(known) == dict(token) for known in issued)

    def revoke(self, grant_id, now=0):
        """Revoke a grant, its tokens, and notify the target user."""
        grant = self._grants[grant_id]
        self._refresh_status(grant, now)
        self._revoke_grant(grant)
        return self._copy_grant(grant)

    def notifications(self, user):
        """Return notification copies addressed to *user*.

        Activation adds a row with kind "impersonation_started" for both the
        target and the agent.
        """
        return [dict(row) for row in self._notifications if row["user"] == user]

    def audit(self):
        """Return audit rows with action, grant_id, agent, target, ticket_id.

        Activation rows use action "activated".
        """
        return [dict(row) for row in self._audit]
