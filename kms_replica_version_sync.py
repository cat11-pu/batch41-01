"""Cross-region KMS version reconciliation."""

import hashlib


class KmsReplicaVersionSync:
    """Reconcile key versions across regions.

    An event is ``{"key", "version", "region", "op", "material"}`` where
    ``op`` is ``put``, ``disable`` or ``delete`` and version is an int or a
    ``vN`` string compared numerically.  Each region has a monotonic version
    vector.  Delete tombstones survive compaction and block resurrection.
    Conflicting writes from two regions resolve deterministically in favour of
    the lexicographically smaller region id and are recorded in ``conflicts``.
    A read is routed to any region that already has the newest version.
    """

    def __init__(self, regions=("r1", "r2", "r3")):
        self.regions = tuple(regions)
        self.history = {}
        self.vector = {region: 0 for region in self.regions}
        self.conflicts = []
        self.audit = []
        self._tombstones = set()
        self._compacted = set()

    _OP_ORDER = {"put": 0, "disable": 1, "delete": 2}

    @staticmethod
    def _parse_version(version):
        if isinstance(version, bool):
            return None
        if isinstance(version, int):
            return version if version > 0 else None
        if isinstance(version, str):
            text = version.strip()
            if text[:1] in ("v", "V"):
                text = text[1:]
            if text.isdigit():
                number = int(text)
                return number if number > 0 else None
        return None

    def _audit(self, event, version, code):
        self.audit.append(
            {
                "key": event.get("key"),
                "version": version,
                "region": event.get("region"),
                "op": event.get("op", "put"),
                "code": code,
            }
        )

    def _reject(self, event, code, version=None):
        self._audit(event, version, code)
        return {"ok": False, "code": code, "version": version}

    def _advance_vector(self, region, version):
        if region in self.vector:
            self.vector[region] = max(self.vector[region], version)

    def apply_event(self, event):
        key = event.get("key")
        region = event.get("region")
        op = event.get("op", "put")
        version = self._parse_version(event.get("version"))
        if version is None:
            return self._reject(event, "invalid_version")
        if op not in self._OP_ORDER:
            return self._reject(event, "unknown_op", version)
        material = event.get("material")
        records = self.history.setdefault(key, {})

        if op == "put" and (key, version) in self._tombstones:
            return self._reject(event, "tombstoned", version)

        existing = records.get(version)
        if existing is not None:
            result = self._apply_to_existing(key, version, existing, event)
        elif op == "put":
            current = max(records) if records else None
            if current is not None and version < current:
                return self._reject(event, "version_rollback", version)
            records[version] = {
                "version": version,
                "material": material,
                "region": region,
                "status": "active",
            }
            result = {"ok": True, "code": "put", "version": version}
        else:
            status = "disabled" if op == "disable" else "deleted"
            records[version] = {
                "version": version,
                "material": None,
                "region": region,
                "status": status,
            }
            if op == "delete":
                self._tombstones.add((key, version))
            result = {"ok": True, "code": op, "version": version}

        self._advance_vector(region, version)
        self._audit(event, version, result["code"])
        return result

    def _apply_to_existing(self, key, version, existing, event):
        op = event.get("op", "put")
        region = event.get("region")
        material = event.get("material")
        if op == "put":
            if existing["material"] == material and existing["status"] == "active":
                return {"ok": True, "code": "duplicate", "version": version}
            if existing["region"] == region:
                return {"ok": False, "code": "version_conflict", "version": version}
            winner = min(existing["region"], region)
            self.conflicts.append(
                {
                    "key": key,
                    "version": version,
                    "regions": sorted([existing["region"], region]),
                    "winner": winner,
                }
            )
            if winner == region:
                existing["material"] = material
                existing["region"] = region
                existing["status"] = "active"
            return {"ok": True, "code": "conflict_resolved", "version": version}
        status = "disabled" if op == "disable" else "deleted"
        if existing["status"] == status:
            return {"ok": True, "code": "duplicate", "version": version}
        if existing["status"] == "deleted":
            return {"ok": True, "code": "duplicate", "version": version}
        existing["status"] = status
        if op == "delete":
            existing["material"] = None
            self._tombstones.add((key, version))
        return {"ok": True, "code": op, "version": version}

    def sync(self, events):
        indexed = list(enumerate(events))
        indexed.sort(
            key=lambda pair: (
                self._parse_version(pair[1].get("version")) or 0,
                self._OP_ORDER.get(pair[1].get("op", "put"), 0),
                pair[0],
            )
        )
        results = [None] * len(events)
        for index, event in indexed:
            results[index] = self.apply_event(event)
        return results

    def read(self, key, version=None, region=None):
        records = self.history.get(key)
        if not records:
            return {"ok": False, "code": "not_found"}
        if version is None:
            version = max(records)
        else:
            version = self._parse_version(version)
            if version is None:
                return {"ok": False, "code": "invalid_version"}
        record = records.get(version)
        if record is None:
            return {"ok": False, "code": "not_found", "version": version}
        if record["status"] == "disabled":
            return {"ok": False, "code": "disabled", "version": version}
        if record["status"] == "deleted":
            return {"ok": False, "code": "deleted", "version": version}
        source = self._route(version, region, record)
        return {
            "ok": True,
            "code": "read",
            "key": key,
            "version": version,
            "material": record["material"],
            "source_region": source,
            "redirected": region is not None and source != region,
        }

    def _route(self, version, region, record):
        if region is not None and self.vector.get(region, 0) >= version:
            return region
        if self.vector.get(record["region"], 0) >= version:
            return record["region"]
        for candidate in sorted(self.vector):
            if self.vector[candidate] >= version:
                return candidate
        return record["region"]

    def current_version(self, key):
        records = self.history.get(key)
        return max(records) if records else None

    def versions(self, key):
        records = self.history.get(key)
        return sorted(records) if records else []

    def lag(self, region):
        if not self.vector:
            return 0
        return max(self.vector.values()) - self.vector.get(region, 0)

    def compact(self):
        for key, records in self.history.items():
            for version, record in records.items():
                if record["status"] == "deleted":
                    self._tombstones.add((key, version))
            self._compacted.add(key)
        return {"ok": True, "tombstones": len(self._tombstones)}

    def import_material(self, key, version, material, checksum):
        payload = material if isinstance(material, bytes) else str(material).encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != checksum:
            return {"ok": False, "code": "corrupt_material"}
        return self.apply_event(
            {
                "key": key,
                "version": version,
                "region": "import",
                "op": "put",
                "material": material,
            }
        )
