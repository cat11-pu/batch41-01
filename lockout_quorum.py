"""Account lockout shared by many login instances.

All counters live in a shared :class:`LockoutStore` so every login instance
sees the same state instead of keeping per-process memory.  Every read and
write happens under the store lock, which makes each failure increment
atomic across threads and instances.  Time is always the server-side ``now``
passed by the caller, the lock key is the normalized account only (never a
password hash), and each lock episode is written to the audit log once.
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

    def _effective_tenant(self, tenant):
        effective = self.tenant if tenant is None else tenant
        if effective != self.tenant:
            raise ValueError("tenant_mismatch")
        return effective

    def _normalize(self, account):
        if account is None or not str(account).strip():
            raise ValueError("empty_account")
        normalized = str(account).strip().lower()
        if len(normalized) > self.max_account_len:
            raise ValueError("account_too_long")
        return normalized

    def _prune(self, failures, now):
        return [stamp for stamp in failures if now - stamp < self.window]

    @staticmethod
    def _record_locked(record, now):
        return record is not None and record["locked_until"] > now

    @staticmethod
    def _error_code(account_locked, ip_locked):
        if account_locked:
            return "account_locked"
        if ip_locked:
            return "ip_locked"
        return None

    def _decision(self, account_record, ip_record, now, ignored):
        account_locked = self._record_locked(account_record, now)
        ip_locked = self._record_locked(ip_record, now)
        return {
            "locked": account_locked or ip_locked,
            "account_locked": account_locked,
            "ip_locked": ip_locked,
            "error": self._error_code(account_locked, ip_locked),
            "ignored": ignored,
        }

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
        effective_tenant = self._effective_tenant(tenant)
        normalized = self._normalize(account)
        account_key = (effective_tenant, normalized)
        ip_key = (effective_tenant, ip) if ip else None

        with self.store.lock:
            account_record = self.store.accounts.get(account_key)

            current_version = account_record["version"] if account_record else 0
            if (
                observed_version is not None
                and observed_version != current_version
            ):
                ip_record = self.store.ips.get(ip_key) if ip_key else None
                return self._decision(account_record, ip_record, now, ignored=True)

            if account_record is None:
                account_record = {"failures": [], "locked_until": 0, "version": 0}
                self.store.accounts[account_key] = account_record
            account_record["failures"] = self._prune(account_record["failures"], now)

            ip_record = None
            if ip_key is not None:
                ip_record = self.store.ips.get(ip_key)
                if ip_record is None:
                    ip_record = {"failures": [], "locked_until": 0}
                    self.store.ips[ip_key] = ip_record
                ip_record["failures"] = self._prune(ip_record["failures"], now)

            account_locked = self._record_locked(account_record, now)
            ip_locked = self._record_locked(ip_record, now)

            # While a lock is active, retries are not counted so they can
            # neither extend the lock window nor refill the counters.
            if not account_locked and not ip_locked:
                account_record["failures"].append(now)
                if ip_record is not None:
                    ip_record["failures"].append(now)

                if len(account_record["failures"]) >= self.threshold:
                    account_record["locked_until"] = now + self.lockout
                    account_locked = True
                    self.store.audit.append({
                        "action": "lock",
                        "account": normalized,
                        "tenant": effective_tenant,
                        "at": now,
                    })

                if (
                    ip_record is not None
                    and len(ip_record["failures"]) >= self.ip_threshold
                ):
                    ip_record["locked_until"] = now + self.lockout
                    ip_locked = True

            return self._decision(account_record, ip_record, now, ignored=False)

    def record_success(self, account, ip="", now=0):
        """Clear the account failure count and lock."""
        normalized = self._normalize(account)
        account_key = (self.tenant, normalized)
        with self.store.lock:
            record = self.store.accounts.get(account_key)
            if record is not None:
                record["failures"] = []
                record["locked_until"] = 0

    def is_locked(self, account, ip="", now=0):
        """Return True when the account or the source IP is locked."""
        normalized = self._normalize(account)
        account_key = (self.tenant, normalized)
        ip_key = (self.tenant, ip) if ip else None
        with self.store.lock:
            account_locked = self._record_locked(
                self.store.accounts.get(account_key), now
            )
            ip_locked = False
            if ip_key is not None:
                ip_locked = self._record_locked(self.store.ips.get(ip_key), now)
            return account_locked or ip_locked

    def admin_unlock(self, account, now=0):
        """Clear counters, bump the account version, and return the version."""
        normalized = self._normalize(account)
        account_key = (self.tenant, normalized)
        with self.store.lock:
            record = self.store.accounts.get(account_key)
            if record is None:
                record = {"failures": [], "locked_until": 0, "version": 0}
                self.store.accounts[account_key] = record
            record["failures"] = []
            record["locked_until"] = 0
            record["version"] += 1
            return record["version"]

    def failure_count(self, account, now=0):
        """Return failures still inside the sliding window."""
        normalized = self._normalize(account)
        account_key = (self.tenant, normalized)
        with self.store.lock:
            record = self.store.accounts.get(account_key)
            if record is None:
                return 0
            return len(self._prune(record["failures"], now))

    def stats(self, account, now=0):
        """Return exactly ``{"failures": int, "locked": bool}``."""
        normalized = self._normalize(account)
        account_key = (self.tenant, normalized)
        with self.store.lock:
            record = self.store.accounts.get(account_key)
            if record is None:
                return {"failures": 0, "locked": False}
            return {
                "failures": len(self._prune(record["failures"], now)),
                "locked": self._record_locked(record, now),
            }

    def audit(self):
        """Return audit rows; a lock event is recorded only once.

        A lock row has ``action == "lock"`` and the normalized account.
        """
        with self.store.lock:
            return list(self.store.audit)
