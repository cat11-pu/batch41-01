"""Identity link merging across multiple identity providers.

An account key is the tuple ``(issuer, sub)``.  Account records are dicts
with ``issuer``, ``sub``, ``email`` (lower-case), ``email_verified``,
``tenant``, ``groups`` (list), and ``key``.  ``email`` is only a hint, never
the primary key.
"""

import copy
import itertools


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
        self._merge_ids = itertools.count(1)
        self._fault = None

    @staticmethod
    def _normalize_email(email):
        return (email or "").strip().lower()

    @staticmethod
    def _validate_key_parts(issuer, sub):
        if not issuer or not str(issuer).strip():
            raise ValueError("issuer must be a non-empty string")
        if not sub or not str(sub).strip():
            raise ValueError("sub must be a non-empty string")

    def register(self, issuer, sub, email, email_verified=False, tenant="default", groups=()):
        """Register an account and return a copy of its record.

        Empty issuer or sub raises ``ValueError`` (message contains
        "issuer" or "sub").  The email is stripped and lower-cased.
        """
        self._validate_key_parts(issuer, sub)
        key = (issuer, sub)
        record = {
            "issuer": issuer,
            "sub": sub,
            "email": self._normalize_email(email),
            "email_verified": bool(email_verified),
            "tenant": tenant or self.default_tenant,
            "groups": list(groups),
            "key": key,
        }
        self._accounts[key] = record
        return copy.deepcopy(record)

    def get(self, key):
        """Return an account record copy for an account key, or None."""
        record = self._accounts.get(tuple(key))
        return copy.deepcopy(record) if record is not None else None

    def accounts(self):
        """Return copies of all active account records."""
        return [copy.deepcopy(record) for record in self._accounts.values()]

    def resolve(self, issuer, sub, email, email_verified=False, tenant="default"):
        """Resolve a login to an account record.

        ``(issuer, sub)`` wins.  When the sub is unknown but a verified
        account in the same tenant and issuer owns the same normalized email,
        that account is reused (the sub may have changed on re-registration).
        Otherwise a new account is registered.
        """
        self._validate_key_parts(issuer, sub)
        key = (issuer, sub)
        if key in self._accounts:
            return copy.deepcopy(self._accounts[key])
        normalized = self._normalize_email(email)
        tenant = tenant or self.default_tenant
        if normalized and email_verified:
            for record in self._accounts.values():
                if (
                    record["issuer"] == issuer
                    and record["tenant"] == tenant
                    and record["email"] == normalized
                    and record["email_verified"]
                ):
                    return copy.deepcopy(record)
        return self.register(
            issuer, sub, email, email_verified=email_verified, tenant=tenant
        )

    @staticmethod
    def _conflict(reason, primary_key, secondary_key):
        return {
            "status": "conflict",
            "reason": reason,
            "candidates": [tuple(primary_key), tuple(secondary_key)],
        }

    def plan_merge(self, primary_key, secondary_key, force=False):
        """Return ``{"status": "ok"}`` or a conflict description.

        Conflict reasons: "tenant_mismatch", "email_unverified" (secondary),
        "email_collision" (same normalized email on different issuers).
        A conflict carries ``"candidates"`` with both keys.
        """
        primary_key = tuple(primary_key)
        secondary_key = tuple(secondary_key)
        primary = self._accounts.get(primary_key)
        secondary = self._accounts.get(secondary_key)
        if primary is None or secondary is None:
            return self._conflict("unknown_account", primary_key, secondary_key)
        if primary["tenant"] != secondary["tenant"]:
            return self._conflict("tenant_mismatch", primary_key, secondary_key)
        if not secondary["email_verified"]:
            return self._conflict("email_unverified", primary_key, secondary_key)
        if (
            primary["email"]
            and primary["email"] == secondary["email"]
            and primary["issuer"] != secondary["issuer"]
        ):
            return self._conflict("email_collision", primary_key, secondary_key)
        return {"status": "ok"}

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
        primary_key = tuple(primary_key)
        secondary_key = tuple(secondary_key)
        pair = (primary_key, secondary_key)
        if pair in self._merges:
            return {
                "status": "already_merged",
                "merge_id": self._merges[pair]["merge_id"],
            }
        if force and confirmation != "CONFIRM":
            return {"status": "confirmation_required"}
        plan = self.plan_merge(primary_key, secondary_key, force=force)
        if plan["status"] != "ok":
            return plan

        state_backup = (
            copy.deepcopy(self._accounts),
            copy.deepcopy(self._sessions),
            copy.deepcopy(self._tokens),
            copy.deepcopy(self._history),
            copy.deepcopy(self._merges),
        )
        try:
            return self._commit_merge(primary_key, secondary_key)
        except MergeFailed:
            (
                self._accounts,
                self._sessions,
                self._tokens,
                self._history,
                self._merges,
            ) = state_backup
            raise

    def _commit_merge(self, primary_key, secondary_key):
        primary = self._accounts[primary_key]
        secondary = self._accounts[secondary_key]

        moved = [g for g in secondary["groups"] if g not in primary["groups"]]
        primary["groups"].extend(moved)
        groups_moved = len(moved)

        self._sessions.pop(secondary_key, None)
        self._tokens.pop(secondary_key, None)

        secondary_snapshot = self._accounts.pop(secondary_key)
        primary_snapshot = copy.deepcopy(primary)
        primary_snapshot["groups"] = [
            g for g in primary["groups"] if g not in moved
        ]

        merge_id = "merge-%d" % next(self._merge_ids)
        row = {
            "merge_id": merge_id,
            "primary": primary_key,
            "secondary": secondary_key,
            "email": secondary_snapshot["email"],
            "groups_moved": groups_moved,
            "actions": ["invalidate_sessions", "invalidate_tokens", "move_groups"],
        }
        self._history.append(row)
        self._merges[(primary_key, secondary_key)] = {
            "merge_id": merge_id,
            "primary_snapshot": primary_snapshot,
            "secondary_snapshot": secondary_snapshot,
        }

        if self._fault == "commit":
            self._fault = None
            raise MergeFailed("injected commit failure; transaction rolled back")
        self._fault = None

        return {
            "status": "merged",
            "merge_id": merge_id,
            "groups_moved": groups_moved,
            "primary": primary_key,
            "secondary": secondary_key,
        }

    def unmerge(self, merge_id):
        """Restore both accounts from the merge snapshot.

        Returns ``{"status": "unmerged", "merge_id": merge_id}``.
        """
        for pair, record in list(self._merges.items()):
            if record["merge_id"] != merge_id:
                continue
            primary_key, secondary_key = pair
            self._accounts[primary_key] = copy.deepcopy(record["primary_snapshot"])
            self._accounts[secondary_key] = copy.deepcopy(
                record["secondary_snapshot"]
            )
            del self._merges[pair]
            return {"status": "unmerged", "merge_id": merge_id}
        raise KeyError("unknown merge_id: %r" % (merge_id,))

    def history(self):
        """Return replayable merge rows.

        Each row has merge_id, primary, secondary, email, groups_moved, and
        ``actions`` containing "invalidate_sessions", "invalidate_tokens",
        and "move_groups".
        """
        return copy.deepcopy(self._history)

    def snapshot(self):
        """Return a detached dict of account-key -> account record."""
        return copy.deepcopy(self._accounts)

    def add_session(self, key, token):
        """Bind a login session token to an account key."""
        self._sessions.setdefault(tuple(key), set()).add(token)

    def session_valid(self, key, token):
        """Return True only for a live session of that account."""
        return token in self._sessions.get(tuple(key), set())

    def add_refresh_token(self, key, token):
        """Register a refresh token for an account key."""
        self._tokens.setdefault(tuple(key), set()).add(token)

    def exchange_refresh(self, key, token):
        """Return True when the refresh token may still be exchanged."""
        return token in self._tokens.get(tuple(key), set())

    def inject_fault(self, stage="commit"):
        """Arrange the next merge to fail at the named stage."""
        self._fault = stage
