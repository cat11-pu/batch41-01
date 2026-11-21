"""Broker for seccomp user notifications.

Delivered as a failing shell: every public operation raises
``NotImplementedError`` until the broker is implemented.

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
        raise NotImplementedError

    def add_child(self, parent_pid, child_pid, start_time):
        """Register a child in the parent's thread group."""
        raise NotImplementedError

    def exit_process(self, pid, start_time):
        """Remove a process identity when pid and start time match."""
        raise NotImplementedError

    def is_tracked(self, pid, start_time=None):
        """Return True when the pid (and optional start time) is tracked."""
        raise NotImplementedError

    def tracked_pids(self):
        """Return the sorted tracked pid list."""
        raise NotImplementedError

    def notify(self, notify_id, pid, start_time, syscall, path=None, now_ms=0):
        """Record a pending notification.

        Empty syscalls raise ValueError("empty_syscall").  Reusing a
        notification id raises ``NotifyError("notify_id_reused")``; an
        unknown process raises ``NotifyError("unknown_process")``.
        """
        raise NotImplementedError

    def respond(self, notify_id, pid, start_time, allow=True, ret=0, now_ms=0):
        """Respond to one notification.

        Returns ``{"responded": True, "allow": bool, "ret": int}``.
        Failures: "unknown_notification", "already_responded",
        "notification_timeout" (after ``tick`` marked it), and
        "stale_notification" when the pid was reused by a new process.
        """
        raise NotImplementedError

    def inject_fd(self, notify_id, target_pid, target_start_time, fd, rights=("read",)):
        """Inject an fd after checking the exact target identity.

        Returns ``{"injected": True, "fd": fd, "rights": [...]}``.  A
        target mismatch raises ``NotifyError("target_mismatch")``; rights
        outside ``fd_rights`` raise ``NotifyError("insufficient_rights")``.
        """
        raise NotImplementedError

    def resolve_path(self, notify_id, path, resolver, now_ms=0):
        """Resolve *path* twice through *resolver*.

        ``resolver(path)`` is called for the check and again for the use; a
        different result raises ``NotifyError("path_changed")``.  On success
        the resolved path string is returned.
        """
        raise NotImplementedError

    def tick(self, now_ms):
        """Mark pending notifications past their deadline and return ids."""
        raise NotImplementedError

    def status(self, notify_id):
        """Return pending/responded/timed_out or raise KeyError."""
        raise NotImplementedError

    def pending(self):
        """Return sorted pending notification ids."""
        raise NotImplementedError

    def pending_for_group(self, tgid):
        """Return sorted pending ids belonging to any thread in *tgid*."""
        raise NotImplementedError

    def restart(self):
        """Return ``{"recovered": [ids]}``; pending work is kept."""
        raise NotImplementedError

    def is_covered(self, syscall):
        """Return True for openat, openat2, and io_uring entry points."""
        raise NotImplementedError

    def choose_action(self, actions):
        """Return the highest priority action (kill > trace > notify > allow)."""
        raise NotImplementedError
