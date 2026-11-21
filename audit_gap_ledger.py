"""Bounded audit queue with a durable gap ledger (initial skeleton)."""


class AuditGapLedger:
    """Bounded, prioritised audit intake with gap accounting.

    A record is ``{"id", "seq", "tenant", "subject", "priority", "op"}``.
    Normal and security records use separate bounded lanes.  A record may only
    be acknowledged after the sink accepted it durably.  Dropped sequence
    numbers are recorded on a separate reliable gap channel and coalesced per
    tenant into inclusive intervals.  Consumers deduplicate by ``id`` and see
    one subject's records in sequence order.  Sampling is never applied.
    """

    def __init__(self, capacity=8, security_capacity=4, tenant_quota=100, sink=None):
        self.capacity = capacity
        self.security_capacity = security_capacity
        self.tenant_quota = tenant_quota
        self.sink = sink
        self.queue = []
        self.security_queue = []
        self.spool = []
        self.gap_channel = []
        self._gaps = {}
        self._tenant_counts = {}
        self.delivered_ids = set()

    def append(self, record):
        raise NotImplementedError

    def drain(self):
        raise NotImplementedError

    def flush(self):
        raise NotImplementedError

    def gaps(self):
        raise NotImplementedError

    def persist_gaps(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
