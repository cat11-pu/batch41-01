"""Bounded audit queue with a durable gap ledger."""


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
        self._accepted = 0
        self._acked = 0
        self._dropped = 0
        self._queue_full_drops = 0
        self._quota_drops = 0
        self._deduplicated = 0
        self._flushed = 0
        self._gap_persist_failures = 0

    def append(self, record):
        priority = record.get("priority", "access")
        security = priority == "security"
        lane = self.security_queue if security else self.queue
        limit = self.security_capacity if security else self.capacity
        tenant = record.get("tenant")
        seq = record.get("seq")

        if not security and self._tenant_counts.get(tenant, 0) >= self.tenant_quota:
            self._record_gap(tenant, seq)
            self._dropped += 1
            self._quota_drops += 1
            return {"ok": False, "code": "quota_exceeded", "acked": False}

        if len(lane) >= limit:
            self._record_gap(tenant, seq)
            self._dropped += 1
            self._queue_full_drops += 1
            return {"ok": False, "code": "queue_full", "acked": False}

        lane.append(record)
        self._accepted += 1
        if not security:
            self._tenant_counts[tenant] = self._tenant_counts.get(tenant, 0) + 1

        if self.sink is None:
            self._acked += 1
            return {"ok": True, "code": "ok", "acked": True}

        if self._write_record(record):
            self._acked += 1
            return {"ok": True, "code": "ok", "acked": True}

        lane.pop()
        self.spool.append(record)
        return {"ok": False, "code": "spooled", "acked": False}

    def drain(self):
        security = self.security_queue
        normal = self.queue
        self.security_queue = []
        self.queue = []

        records = self._ordered(security + normal)
        drained = []
        for record in records:
            record_id = record.get("id")
            if record_id is not None and record_id in self.delivered_ids:
                self._deduplicated += 1
                continue
            if record_id is not None:
                self.delivered_ids.add(record_id)
            drained.append(record)

        for record in security + normal:
            if record.get("priority", "access") != "security":
                tenant = record.get("tenant")
                count = self._tenant_counts.get(tenant, 0)
                if count <= 1:
                    self._tenant_counts.pop(tenant, None)
                else:
                    self._tenant_counts[tenant] = count - 1

        return drained

    def flush(self):
        if self.sink is None:
            return 0

        flushed = 0
        remaining = []
        for record in self.spool:
            if not self._write_record(record):
                remaining.append(record)
                continue

            flushed += 1
            self._flushed += 1
            security = record.get("priority", "access") == "security"
            lane = self.security_queue if security else self.queue
            limit = self.security_capacity if security else self.capacity
            if len(lane) < limit:
                lane.append(record)
            elif not security:
                tenant = record.get("tenant")
                count = self._tenant_counts.get(tenant, 0)
                if count <= 1:
                    self._tenant_counts.pop(tenant, None)
                else:
                    self._tenant_counts[tenant] = count - 1

        self.spool = remaining
        return flushed

    def gaps(self):
        result = []
        for tenant, intervals in self._gaps.items():
            for start, end in intervals:
                result.append({"tenant": tenant, "start": start, "end": end})
        result.sort(key=lambda gap: (gap["tenant"], gap["start"], gap["end"]))
        return result

    def persist_gaps(self):
        if self.sink is None:
            return []

        writer = getattr(self.sink, "write_gap", None)
        if writer is None:
            self._gap_persist_failures += len(self.gap_channel)
            return []

        persisted = []
        remaining = []
        for gap in self.gap_channel:
            try:
                written = writer(dict(gap))
            except Exception:
                written = False
            if written:
                persisted.append(dict(gap))
            else:
                remaining.append(gap)
                self._gap_persist_failures += 1
        self.gap_channel = remaining
        return persisted

    def stats(self):
        return {
            "accepted": self._accepted,
            "acked": self._acked,
            "queued": len(self.queue),
            "security_queued": len(self.security_queue),
            "spooled": len(self.spool),
            "gaps": sum(len(intervals) for intervals in self._gaps.values()),
            "dropped": self._dropped,
            "queue_full_drops": self._queue_full_drops,
            "quota_drops": self._quota_drops,
            "deduplicated": self._deduplicated,
            "flushed": self._flushed,
            "pending_gap_markers": len(self.gap_channel),
            "gap_persist_failures": self._gap_persist_failures,
        }

    def _write_record(self, record):
        try:
            return bool(self.sink.write(record))
        except Exception:
            return False

    def _ordered(self, records):
        groups = {}
        order = []
        for record in records:
            subject = record.get("subject")
            if subject not in groups:
                groups[subject] = []
                order.append(subject)
            groups[subject].append(record)

        ordered = []
        for subject in order:
            ordered.extend(sorted(groups[subject], key=lambda item: item.get("seq", 0)))
        return ordered

    def _record_gap(self, tenant, seq):
        if not isinstance(seq, int):
            return

        intervals = self._gaps.setdefault(tenant, [])
        start = seq
        end = seq
        merged = []
        kept = []
        for interval_start, interval_end in intervals:
            if interval_start <= end + 1 and start <= interval_end + 1:
                start = min(start, interval_start)
                end = max(end, interval_end)
                merged.append((interval_start, interval_end))
            else:
                kept.append([interval_start, interval_end])

        if merged:
            self.gap_channel = [
                marker
                for marker in self.gap_channel
                if not (
                    marker.get("tenant") == tenant
                    and (marker.get("start"), marker.get("end")) in merged
                )
            ]

        kept.append([start, end])
        kept.sort()
        self._gaps[tenant] = kept
        self.gap_channel.append({"tenant": tenant, "start": start, "end": end})
