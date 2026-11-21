"""Broker for seccomp user notifications.

Notifications are matched by notification id plus the target pid and its
start time.  Responses are per notification id, never FIFO.  Path checks
resolve twice to catch a swap between check and use.  Pending notifications
survive a supervisor restart.
"""


class NotifyError(Exception):
    """Notification failure carrying a stable ``reason`` code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SeccompNotifyBroker:
    """Track seccomp notifications, responses, and fd injection."""

    ACTION_PRIORITY = {"kill": 0, "trace": 1, "notify": 2, "allow": 3}
    COVERED_SYSCALLS = frozenset(
        {"openat", "openat2", "io_uring_enter", "io_uring_setup"}
    )

    def __init__(self, timeout_ms=500, fd_rights=("read",)):
        self.timeout_ms = timeout_ms
        self.fd_rights = tuple(fd_rights)
        self._processes = {}
        self._notifications = {}

    def register_process(self, pid, start_time, tgid=None):
        """Register a thread/process identity; tgid defaults to pid."""
        if tgid is None:
            tgid = pid
        self._processes[pid] = {"start_time": start_time, "tgid": tgid}

    def add_child(self, parent_pid, child_pid, start_time):
        """Register a child in the parent's thread group."""
        parent = self._processes.get(parent_pid)
        if parent is None:
            raise NotifyError("unknown_process")
        self._processes[child_pid] = {
            "start_time": start_time,
            "tgid": parent["tgid"],
        }

    def exit_process(self, pid, start_time):
        """Remove a process identity when pid and start time match."""
        process = self._processes.get(pid)
        if process is not None and process["start_time"] == start_time:
            del self._processes[pid]

    def is_tracked(self, pid, start_time=None):
        """Return True when the pid (and optional start time) is tracked."""
        process = self._processes.get(pid)
        if process is None:
            return False
        if start_time is not None and process["start_time"] != start_time:
            return False
        return True

    def tracked_pids(self):
        """Return the sorted tracked pid list."""
        return sorted(self._processes)

    def notify(self, notify_id, pid, start_time, syscall, path=None, now_ms=0):
        """Record a pending notification.

        Empty syscalls raise ValueError("empty_syscall").  Reusing a
        notification id raises ``NotifyError("notify_id_reused")``; an
        unknown process raises ``NotifyError("unknown_process")``.
        """
        if not syscall:
            raise ValueError("empty_syscall")
        if notify_id in self._notifications:
            raise NotifyError("notify_id_reused")
        process = self._processes.get(pid)
        if process is None or process["start_time"] != start_time:
            raise NotifyError("unknown_process")
        self._notifications[notify_id] = {
            "pid": pid,
            "start_time": start_time,
            "tgid": process["tgid"],
            "syscall": syscall,
            "path": path,
            "status": "pending",
            "deadline": now_ms + self.timeout_ms,
            "allow": None,
            "ret": None,
            "resolved_path": None,
            "injected_fds": [],
        }

    def respond(self, notify_id, pid, start_time, allow=True, ret=0, now_ms=0):
        """Respond to one notification.

        Returns ``{"responded": True, "allow": bool, "ret": int}``.
        Failures: "unknown_notification", "already_responded",
        "notification_timeout" (after ``tick`` marked it), and
        "stale_notification" when the pid was reused by a new process.
        """
        record = self._notifications.get(notify_id)
        if record is None:
            raise NotifyError("unknown_notification")
        if record["status"] == "responded":
            raise NotifyError("already_responded")
        if record["status"] == "timed_out":
            raise NotifyError("notification_timeout")
        if not self._identity_matches(record, pid, start_time):
            raise NotifyError("stale_notification")
        record["status"] = "responded"
        record["allow"] = allow
        record["ret"] = ret
        return {"responded": True, "allow": allow, "ret": ret}

    def _identity_matches(self, record, pid, start_time):
        if pid != record["pid"] or start_time != record["start_time"]:
            return False
        return self.is_tracked(pid, start_time)

    def inject_fd(self, notify_id, target_pid, target_start_time, fd, rights=("read",)):
        """Inject an fd after checking the exact target identity.

        Returns ``{"injected": True, "fd": fd, "rights": [...]}``.  A
        target mismatch raises ``NotifyError("target_mismatch")``; rights
        outside ``fd_rights`` raise ``NotifyError("insufficient_rights")``.
        """
        record = self._notifications.get(notify_id)
        if record is None:
            raise NotifyError("unknown_notification")
        if not self._identity_matches(record, target_pid, target_start_time):
            raise NotifyError("target_mismatch")
        rights = tuple(rights)
        if any(right not in self.fd_rights for right in rights):
            raise NotifyError("insufficient_rights")
        record["injected_fds"].append({"fd": fd, "rights": rights})
        return {"injected": True, "fd": fd, "rights": list(rights)}

    def resolve_path(self, notify_id, path, resolver, now_ms=0):
        """Resolve *path* twice through *resolver*.

        ``resolver(path)`` is called for the check and again for the use; a
        different result raises ``NotifyError("path_changed")``.  On success
        the resolved path string is returned.
        """
        record = self._notifications.get(notify_id)
        if record is None:
            raise NotifyError("unknown_notification")
        checked = resolver(path)
        used = resolver(path)
        if checked != used:
            raise NotifyError("path_changed")
        record["resolved_path"] = path
        return path

    def tick(self, now_ms):
        """Mark pending notifications past their deadline and return ids."""
        timed_out = []
        for notify_id, record in self._notifications.items():
            if record["status"] == "pending" and now_ms > record["deadline"]:
                record["status"] = "timed_out"
                timed_out.append(notify_id)
        return sorted(timed_out)

    def status(self, notify_id):
        """Return pending/responded/timed_out or raise KeyError."""
        return self._notifications[notify_id]["status"]

    def pending(self):
        """Return sorted pending notification ids."""
        return sorted(
            notify_id
            for notify_id, record in self._notifications.items()
            if record["status"] == "pending"
        )

    def pending_for_group(self, tgid):
        """Return sorted pending ids belonging to any thread in *tgid*."""
        return sorted(
            notify_id
            for notify_id, record in self._notifications.items()
            if record["status"] == "pending" and record["tgid"] == tgid
        )

    def restart(self):
        """Return ``{"recovered": [ids]}``; pending work is kept."""
        return {"recovered": self.pending()}

    def is_covered(self, syscall):
        """Return True for openat, openat2, and io_uring entry points."""
        return syscall in self.COVERED_SYSCALLS

    def choose_action(self, actions):
        """Return the highest priority action (kill > trace > notify > allow)."""
        return min(actions, key=lambda action: self.ACTION_PRIORITY[action])
