"""Process lineage gate (initial skeleton)."""


class ProcessLineageGate:
    """Build and judge process ancestry from exec/exit events.

    An event is ``{"pid", "ppid", "start", "name", "exe", "exe_hash",
    "namespace", "cmdline", "session", "seq", "ts", "kind"}``.  The process key
    is ``(namespace, pid, start)``.  A parent is the process with matching pid
    in the same namespace whose start time is the greatest one not after the
    child start.  Whitelist entries are dicts with exact ``name``, ``exe`` and
    ``cmdline`` values.

    ``lineage`` returns ``{"chain", "depth", "truncated", "expired", "missing"}``
    with the target first.  ``verdict`` adds ``{"trusted", "reason"}``.
    """

    def __init__(self, whitelist=(), max_depth=8, retention=60.0):
        self.whitelist = list(whitelist)
        self.max_depth = max_depth
        self.retention = retention

    def ingest(self, events):
        raise NotImplementedError

    def lineage(self, pid, start=None, namespace="host", now=None):
        raise NotImplementedError

    def verdict(self, pid, start=None, namespace="host", now=None):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
