"""Process lineage gate."""


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
        self.whitelist = [dict(entry) for entry in whitelist]
        self.max_depth = max_depth
        self.retention = retention
        self._processes = {}
        self._by_pid = {}
        self._seen_events = set()
        self._event_count = 0

    @staticmethod
    def _fingerprint(event):
        return tuple(sorted((key, repr(value)) for key, value in event.items()))

    def ingest(self, events):
        accepted = 0
        for event in events:
            fingerprint = self._fingerprint(event)
            if fingerprint in self._seen_events:
                continue
            self._seen_events.add(fingerprint)
            self._event_count += 1
            accepted += 1

            namespace = event.get("namespace", "host")
            start = float(event["start"])
            ts = float(event.get("ts", start))
            key = (namespace, event["pid"], start)
            record = self._processes.get(key)
            if record is None:
                record = {
                    "pid": event["pid"],
                    "ppid": event.get("ppid"),
                    "start": start,
                    "name": event.get("name"),
                    "exe": event.get("exe"),
                    "exe_hash": event.get("exe_hash"),
                    "namespace": namespace,
                    "cmdline": list(event.get("cmdline") or []),
                    "session": event.get("session"),
                    "exit_ts": None,
                    "last_seen": ts,
                }
                self._processes[key] = record
                self._by_pid.setdefault((namespace, record["pid"]), []).append(record)
            record["last_seen"] = max(record["last_seen"], ts)
            if event.get("kind") == "exit":
                if record["exit_ts"] is None or ts > record["exit_ts"]:
                    record["exit_ts"] = ts
        return accepted

    def _resolve(self, pid, start, namespace):
        if start is not None:
            return self._processes.get((namespace, pid, float(start)))
        candidates = self._by_pid.get((namespace, pid), [])
        if not candidates:
            return None
        return max(candidates, key=lambda record: record["start"])

    def _is_expired(self, record, now):
        return now is not None and now - record["last_seen"] > self.retention

    def _find_parent(self, record, now):
        if record["ppid"] is None:
            return None
        best = None
        for candidate in self._by_pid.get((record["namespace"], record["ppid"]), []):
            if candidate is record or candidate["start"] > record["start"]:
                continue
            if self._is_expired(candidate, now):
                continue
            if best is None or candidate["start"] > best["start"]:
                best = candidate
        return best

    @staticmethod
    def _public(record):
        return {
            "pid": record["pid"],
            "ppid": record["ppid"],
            "start": record["start"],
            "name": record["name"],
            "exe": record["exe"],
            "exe_hash": record["exe_hash"],
            "namespace": record["namespace"],
            "cmdline": list(record["cmdline"]),
            "session": record["session"],
        }

    def lineage(self, pid, start=None, namespace="host", now=None):
        result = {
            "chain": [],
            "depth": 0,
            "truncated": False,
            "expired": False,
            "missing": False,
        }
        record = self._resolve(pid, start, namespace)
        if record is None:
            result["missing"] = True
            return result
        if self._is_expired(record, now):
            result["expired"] = True
            return result

        visited = set()
        current = record
        while current is not None and len(result["chain"]) < self.max_depth:
            key = (current["namespace"], current["pid"], current["start"])
            if key in visited:
                current = None
                break
            visited.add(key)
            result["chain"].append(self._public(current))
            current = self._find_parent(current, now)
        result["truncated"] = current is not None
        result["depth"] = len(result["chain"])
        return result

    def verdict(self, pid, start=None, namespace="host", now=None):
        result = self.lineage(pid, start, namespace, now)
        record = self._resolve(pid, start, namespace)
        trusted = False
        if result["missing"]:
            reason = "missing_process"
        elif result["expired"]:
            reason = "expired"
        else:
            reason = "not_whitelisted"
            for entry in self.whitelist:
                if (
                    entry.get("name") == record["name"]
                    and entry.get("exe") == record["exe"]
                    and list(entry.get("cmdline") or []) == record["cmdline"]
                ):
                    trusted = True
                    reason = "whitelisted"
                    break
        result["trusted"] = trusted
        result["reason"] = reason
        return result

    def stats(self):
        return {"events": self._event_count, "processes": len(self._processes)}
