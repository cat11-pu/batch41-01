"""NTP clock skew guard.

Tracks a steerable wall clock separately from a monotonic clock that is never
steered, selects the most trustworthy NTP source, fails closed while the time
is unsynchronised or beyond the configured skew threshold, and audits every
time-steering decision.
"""

import threading
from datetime import datetime, timezone


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
        self.max_offset = float(max_offset)
        self.tz = tz
        self.synced = False
        self.skew_exceeded = False
        self.audit = []
        self.last_audit_ts = None
        self._lock = threading.RLock()

    def wall_now(self):
        """Return the current (steerable) wall clock value."""
        with self._lock:
            return self.wall

    def mono_now(self):
        """Return the monotonic clock value, which can never be steered."""
        with self._lock:
            return self.mono

    def advance_mono(self, seconds):
        """Advance the monotonic clock; the wall clock is unaffected."""
        with self._lock:
            self.mono += float(seconds)

    def apply_step(self, new_wall):
        """Apply an explicit wall-clock step and report any jump.

        A jump is any change whose magnitude exceeds ``max_offset``.  The
        monotonic clock is untouched, so callers can still tell how much real
        time elapsed.
        """
        with self._lock:
            new_wall = float(new_wall)
            delta = new_wall - self.wall
            jumped = abs(delta) > self.max_offset
            self.wall = new_wall
            result = {
                "jump": jumped,
                "delta": delta,
                "wall": self.wall,
                "mono": self.mono,
            }
            self.audit.append(
                {
                    "code": "wall_step",
                    "jump": jumped,
                    "delta": delta,
                    "wall": self.wall,
                }
            )
            return result

    def snapshot(self):
        """Atomically capture wall and monotonic time.

        The returned dict is a detached copy; mutating it cannot affect the
        internal state of the guard.
        """
        with self._lock:
            return {
                "wall": self.wall,
                "mono": self.mono,
                "synced": self.synced,
                "skew_exceeded": self.skew_exceeded,
                "tz": self.tz,
            }

    @staticmethod
    def _offset(response):
        t1 = response["t1"]
        t2 = response["t2"]
        t3 = response["t3"]
        t4 = response["t4"]
        return ((t2 - t1) + (t3 - t4)) / 2.0

    @staticmethod
    def _delay(response):
        t1 = response["t1"]
        t2 = response["t2"]
        t3 = response["t3"]
        t4 = response["t4"]
        return max(0.0, (t4 - t1) - (t3 - t2))

    @staticmethod
    def _score(response, delay):
        # Trustworthiness first: stratum, then stability (jitter), then the
        # round-trip delay, with id as a deterministic tie-breaker.  Response
        # speed alone never wins over a better stratum.
        return (
            response["stratum"],
            response["jitter"],
            delay,
            response["id"],
        )

    def sync(self, responses):
        """Process NTP responses and steer the wall clock.

        Unreachable sources are ignored.  The most trustworthy reachable
        source is selected; an offset beyond ``max_offset`` fails closed
        without steering the clock.  Every attempt is audited.
        """
        with self._lock:
            reachable = [
                response
                for response in responses
                if response.get("reachable", True)
            ]
            if not reachable:
                result = {"ok": False, "code": "no_source"}
                self.audit.append(dict(result))
                return result

            chosen = min(
                reachable,
                key=lambda response: self._score(
                    response, self._delay(response)
                ),
            )
            source = chosen["id"]
            offset = self._offset(chosen)
            delay = self._delay(chosen)

            if abs(offset) > self.max_offset:
                self.skew_exceeded = True
                self.synced = False
                result = {
                    "ok": False,
                    "code": "skew_exceeded",
                    "source": source,
                    "offset": offset,
                    "delay": delay,
                    "skew_exceeded": True,
                }
                self.audit.append(dict(result))
                return result

            self.wall += offset
            self.synced = True
            self.skew_exceeded = False
            result = {
                "ok": True,
                "source": source,
                "offset": offset,
                "delay": delay,
                "skew_exceeded": False,
            }
            self.audit.append(dict(result))
            return result

    def certificate_valid(self, not_before, not_after):
        """Validate a certificate against the wall clock.

        Fails closed until the time has been synchronised at least once.
        """
        with self._lock:
            if not self.synced or self.skew_exceeded:
                return False
            return not_before <= self.wall <= not_after

    def sensitive_ready(self):
        """Sensitive operations are allowed only on a trusted, in-range clock."""
        with self._lock:
            return self.synced and not self.skew_exceeded

    def request_allowed(self):
        """Requests are rejected once the measured skew is out of range."""
        with self._lock:
            return not self.skew_exceeded

    def normalize_timestamp(self, timestamp, leap=False):
        """Clamp a timestamp's seconds field.

        During a leap second the value 60 is smeared back into the final
        second of the minute instead of overflowing the next window.
        """
        if leap and timestamp >= 60.0:
            return 60.0 - 1e-9
        return float(timestamp)

    def task_due(self, last_run_mono, interval):
        """Decide task scheduling from monotonic time only.

        Wall-clock jumps therefore cannot trigger repeated or missed runs.
        """
        with self._lock:
            return self.mono - last_run_mono >= interval

    def audit_timestamp(self):
        """Return a timestamp that can never move backwards.

        A wall-clock rollback is absorbed by holding the high-water mark, so
        audit chains keep their ordering even while the raw wall clock moves.
        """
        with self._lock:
            if self.last_audit_ts is None or self.wall > self.last_audit_ts:
                self.last_audit_ts = self.wall
            return self.last_audit_ts

    def format_log_time(self, timestamp):
        """Format any timestamp as UTC, ignoring the container's timezone."""
        return (
            datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
