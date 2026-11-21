"""NTP clock skew guard (initial skeleton)."""


class ClockSkewGuard:
    """Track wall and monotonic time and guard time-dependent decisions.

    An NTP response is ``{"id", "stratum", "jitter", "t1", "t2", "t3", "t4",
    "reachable"}``.  Offset is ``((t2-t1)+(t3-t4))/2`` and delay is
    ``(t4-t1)-(t3-t2)`` clamped at zero.  Sources are selected by
    ``(stratum, jitter, delay, id)``.  Wall clock is steered, monotonic time is
    never steered.  Unsynchronised time fails closed for sensitive actions and
    certificates.  ``audit_timestamp`` never moves backwards.
    """

    def __init__(self, wall=0.0, mono=0.0, max_offset=1.0, tz="+00:00"):
        self.wall = float(wall)
        self.mono = float(mono)
        self.max_offset = max_offset
        self.tz = tz
        self.synced = False
        self.skew_exceeded = False
        self.audit = []
        self.last_audit_ts = None

    def wall_now(self):
        raise NotImplementedError

    def mono_now(self):
        raise NotImplementedError

    def advance_mono(self, seconds):
        raise NotImplementedError

    def apply_step(self, new_wall):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def sync(self, responses):
        raise NotImplementedError

    def certificate_valid(self, not_before, not_after):
        raise NotImplementedError

    def sensitive_ready(self):
        raise NotImplementedError

    def request_allowed(self):
        raise NotImplementedError

    def normalize_timestamp(self, timestamp, leap=False):
        raise NotImplementedError

    def task_due(self, last_run_mono, interval):
        raise NotImplementedError

    def audit_timestamp(self):
        raise NotImplementedError

    def format_log_time(self, timestamp):
        raise NotImplementedError
