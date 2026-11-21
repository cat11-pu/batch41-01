import hashlib


class ZoneTransferGuard:
    """AXFR/IXFR transaction, journal cursor and TSIG validation."""

    SERIAL_MOD = 1 << 32
    SERIAL_HALF = 1 << 31

    def __init__(self, zone='example.internal', allowed_sources=None, max_records=1000, tsig_key='tsig-secret', tsig_fudge=300, journal_retention=3):
        self.zone = zone
        self.allowed_sources = set(allowed_sources) if allowed_sources else set()
        self.max_records = max_records
        self.tsig_key = tsig_key
        self.tsig_fudge = tsig_fudge
        self.journal_retention = journal_retention
        self._records = {}
        self._serial = 0
        self._journal = {}
        self._journal_order = []
        self._cursors = {}
        self._last_notify = None

    @staticmethod
    def serial_gt(left, right):
        diff = (left - right) % ZoneTransferGuard.SERIAL_MOD
        return 0 < diff < ZoneTransferGuard.SERIAL_HALF

    def load_zone(self, records, serial=0):
        staged = {}
        for record in records:
            key = (record["name"], record.get("type", "A"))
            staged[key] = {k: v for k, v in record.items() if k != "op"}
        self._records = staged
        self._serial = serial
        self._journal = {}
        self._journal_order = []

    def current_serial(self):
        return self._serial

    def zone_records(self):
        return [dict(record) for record in self._records.values()]

    def _signature(self, serial, seq):
        digest = hashlib.sha256(
            "{}:{}:{}".format(self.tsig_key, serial, seq).encode("utf-8")
        ).hexdigest()
        return digest[:16]

    def _journal_chain(self, client_serial):
        chain = []
        serial = self._serial
        steps = 0
        while self.serial_gt(serial, client_serial):
            entry = self._journal.get(serial)
            if entry is None:
                return None
            chain.append(entry)
            serial = entry["base"]
            steps += 1
            if steps > len(self._journal):
                return None
        if serial != client_serial:
            return None
        chain.reverse()
        return chain

    def _journal_covers(self, client_serial):
        return self._journal_chain(client_serial) is not None

    def handle_request(self, request):
        if request.get("zone") != self.zone:
            return {"ok": False, "reason": "zone mismatch"}
        source = request.get("source")
        if self.allowed_sources and source not in self.allowed_sources:
            return {"ok": False, "reason": "source not allowed"}
        if request.get("type") == "IXFR":
            client_serial = request.get("serial", 0)
            chain = self._journal_chain(client_serial)
            if chain is not None:
                return {
                    "ok": True,
                    "type": "IXFR",
                    "serial": self._serial,
                    "diffs": [
                        {"serial": entry["serial"], "records": entry["records"]}
                        for entry in chain
                    ],
                }
            return {
                "ok": True,
                "type": "AXFR",
                "fallback": True,
                "serial": self._serial,
                "records": self.zone_records(),
            }
        return {
            "ok": True,
            "type": "AXFR",
            "serial": self._serial,
            "records": self.zone_records(),
        }

    def apply_transfer(self, messages, from_addr=None, now=0.0):
        if self.allowed_sources and from_addr not in self.allowed_sources:
            return {"applied": False, "reason": "source not allowed"}
        if not messages:
            return {"applied": False, "reason": "empty transfer"}
        ordered = sorted(messages, key=lambda message: message.get("seq", 0))
        total = sum(len(message.get("records", [])) for message in ordered)
        if total > self.max_records:
            return {"applied": False, "reason": "too many records"}
        for message in ordered:
            tsig_time = message.get("tsig_time")
            if tsig_time is None or abs(now - tsig_time) > self.tsig_fudge:
                return {"applied": False, "reason": "tsig time out of window"}
            expected = self._signature(message.get("serial"), message.get("seq"))
            if message.get("tsig") != expected:
                return {"applied": False, "reason": "bad tsig"}
        full_replace = any(message.get("axfr") for message in ordered)
        staged_records = {} if full_replace else dict(self._records)
        staged_serial = self._serial
        staged_entries = []
        for message in ordered:
            for record in message.get("records", []):
                key = (record["name"], record.get("type", "A"))
                if record.get("op", "add") == "delete":
                    staged_records.pop(key, None)
                else:
                    staged_records[key] = {
                        k: v for k, v in record.items() if k != "op"
                    }
            msg_serial = message.get("serial")
            if msg_serial is not None and self.serial_gt(msg_serial, staged_serial):
                staged_entries.append({
                    "serial": msg_serial,
                    "base": staged_serial,
                    "records": message.get("records", []),
                })
                staged_serial = msg_serial
        self._records = staged_records
        self._serial = staged_serial
        for entry in staged_entries:
            self._journal[entry["serial"]] = entry
            self._journal_order.append(entry["serial"])
        while len(self._journal_order) > self.journal_retention:
            expired = self._journal_order.pop(0)
            self._journal.pop(expired, None)
        return {"applied": True, "serial": self._serial}

    def notify(self, serial, source=None):
        if self.allowed_sources and source not in self.allowed_sources:
            return {"accepted": False, "reason": "source not allowed"}
        self._last_notify = {"serial": serial, "source": source}
        return {"accepted": True}

    def refresh(self, slave_serial):
        return {
            "needed": self.serial_gt(self._serial, slave_serial),
            "serial": self._serial,
        }

    def advance_cursor(self, slave, serial):
        current = self._cursors.get(slave)
        if current is None or self.serial_gt(serial, current):
            self._cursors[slave] = serial
            return {"advanced": True, "cursor": serial}
        return {"advanced": False, "cursor": current}

    def compression_name(self, data, offset=0):
        labels = []
        visited = {offset}
        pos = offset
        while True:
            if pos >= len(data):
                raise ValueError("truncated name")
            length = data[pos]
            if length & 0xC0 == 0xC0:
                if pos + 1 >= len(data):
                    raise ValueError("truncated compression pointer")
                pointer = ((length & 0x3F) << 8) | data[pos + 1]
                if pointer in visited:
                    raise ValueError("compression pointer loop")
                visited.add(pointer)
                pos = pointer
                continue
            if length & 0xC0:
                raise ValueError("reserved label type")
            if length == 0:
                break
            pos += 1
            if pos + length > len(data):
                raise ValueError("truncated label")
            labels.append(data[pos:pos + length].decode("ascii", "replace"))
            pos += length
        return ".".join(labels)

    def snapshot(self):
        return {
            "zone": self.zone,
            "serial": self._serial,
            "cursors": dict(self._cursors),
            "journal": list(self._journal_order),
        }
