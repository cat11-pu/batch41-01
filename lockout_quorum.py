"""Account lockout shared by many login instances.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the lockout policy is implemented.

All counters live in a :class:`LockoutStore` so instances do not keep local
memory.  The lock key is the normalized account only, never a password hash.
Updates must be atomic because many threads and instances share a store.
"""

import threading


class LockoutStore:
    """Shared counter store; one instance may be shared by many workers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.accounts = {}
        self.ips = {}
        self.audit = []


class LockoutQuorum:
    """Count failures and lock accounts/IPs using server-side time."""

    def __init__(
        self,
        store=None,
        threshold=5,
        ip_threshold=5,
        window=300,
        lockout=900,
        tenant="default",
        max_account_len=64,
    ):
        self.store = store if store is not None else LockoutStore()
        self.threshold = threshold
        self.ip_threshold = ip_threshold
        self.window = window
        self.lockout = lockout
        self.tenant = tenant
        self.max_account_len = max_account_len

    def record_failure(self, account, ip="", now=0, password_hash=None, tenant=None, observed_version=None):
        """Record one failure and return the lock decision.

        Result: ``{"locked": bool, "account_locked": bool,
        "ip_locked": bool, "error": "account_locked"|"ip_locked"|None,
        "ignored": bool}``.  Empty accounts raise ValueError("empty_account"),
        over-long accounts ValueError("account_too_long"), a foreign tenant
        ValueError("tenant_mismatch").  When *observed_version* does not
        match the current account version the failure is ignored, which
        prevents an async worker from refilling counters after an admin
        unlock.
        """
        raise NotImplementedError

    def record_success(self, account, ip="", now=0):
        """Clear the account failure count and lock."""
        raise NotImplementedError

    def is_locked(self, account, ip="", now=0):
        """Return True when the account or the source IP is locked."""
        raise NotImplementedError

    def admin_unlock(self, account, now=0):
        """Clear counters, bump the account version, and return the version."""
        raise NotImplementedError

    def failure_count(self, account, now=0):
        """Return failures still inside the sliding window."""
        raise NotImplementedError

    def stats(self, account, now=0):
        """Return exactly ``{"failures": int, "locked": bool}``."""
        raise NotImplementedError

    def audit(self):
        """Return audit rows; a lock event is recorded only once.

        A lock row has ``action == "lock"`` and the normalized account.
        """
        raise NotImplementedError
