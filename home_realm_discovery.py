import copy

try:
    import idna as _idna
except ImportError:  # pragma: no cover - the stdlib codec is a fallback only
    _idna = None


class RealmDiscoveryIndex:
    def __init__(self, cache_ttl=600):
        self.cache_ttl = cache_ttl
        self.entries = []
        self.version = 0
        self.cache = {}

    def normalize_domain(self, domain):
        if not isinstance(domain, str):
            raise ValueError("domain must be a string")

        domain = domain.strip()
        for dot in ("\u3002", "\uff0e", "\uff61"):
            domain = domain.replace(dot, ".")
        if domain.endswith("."):
            domain = domain[:-1]
        if not domain:
            raise ValueError("domain is empty")

        labels = domain.split(".")
        if any(not label for label in labels):
            raise ValueError("domain contains an empty label")

        normalized = []
        for position, label in enumerate(labels):
            if "*" in label:
                if position != 0 or label != "*":
                    raise ValueError("wildcard is only allowed as the first label")
                normalized.append("*")
                continue

            ascii_label = self._label_to_ascii(label)
            normalized.append(ascii_label)

        if normalized[0] == "*" and len(normalized) == 1:
            raise ValueError("wildcard must have a suffix")

        result = ".".join(normalized)
        if len(result) > 253:
            raise ValueError("domain is too long")
        return result

    def _label_to_ascii(self, label):
        if not label:
            raise ValueError("domain contains an empty label")

        lowered = label.lower()
        try:
            if _idna is not None:
                ascii_label = _idna.encode(
                    lowered, uts46=True, std3_rules=True
                ).decode("ascii")
            else:
                ascii_label = lowered.encode("idna").decode("ascii")
        except Exception as exc:
            raise ValueError("invalid domain label") from exc

        ascii_label = ascii_label.lower()
        if (
            not ascii_label
            or len(ascii_label) > 63
            or ascii_label.startswith("-")
            or ascii_label.endswith("-")
            or "." in ascii_label
        ):
            raise ValueError("invalid domain label")
        return ascii_label

    def add_domain(self, tenant_id, domain, idp_id, verified=False, wildcard=False,
                   priority=0, active=True):
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(idp_id, str) or not idp_id:
            raise ValueError("idp_id must be a non-empty string")

        normalized = self.normalize_domain(domain)
        is_wildcard = normalized.startswith("*.")
        if wildcard and not is_wildcard:
            raise ValueError("wildcard registration requires a *.domain name")
        if is_wildcard and not wildcard:
            raise ValueError("wildcard domain requires wildcard=True")

        try:
            priority = int(priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("priority must be an integer") from exc

        self.entries.append(
            {
                "tenant_id": tenant_id,
                "domain": normalized,
                "idp_id": idp_id,
                "verified": bool(verified),
                "wildcard": bool(wildcard),
                "priority": priority,
                "active": bool(active),
            }
        )
        self.version += 1
        return len(self.entries)

    def remove_tenant(self, tenant_id):
        before = len(self.entries)
        self.entries = [
            entry for entry in self.entries if entry["tenant_id"] != tenant_id
        ]
        removed = before - len(self.entries)
        if removed:
            self.version += 1
        return removed

    def resolve(self, email, now=0):
        if not isinstance(email, str) or email.count("@") != 1:
            return self._error("invalid_email")

        local_part, domain = email.rsplit("@", 1)
        if not local_part or not local_part.strip() or local_part != local_part.strip():
            return self._error("invalid_email")
        if not domain:
            return self._error("invalid_domain")

        try:
            normalized_domain = self.normalize_domain(domain)
        except ValueError:
            return self._error("invalid_domain")
        if "*" in normalized_domain:
            return self._error("invalid_domain")

        cached = self.cache.get(normalized_domain)
        if cached is not None and cached["version"] == self.version:
            age = now - cached["at"]
            if 0 <= age < self.cache_ttl:
                result = copy.deepcopy(cached["result"])
                result["cached"] = True
                return result

        result = self._resolve_domain(normalized_domain)
        result["cached"] = False
        result["version"] = self.version
        if self.cache_ttl > 0:
            self.cache[normalized_domain] = {
                "version": self.version,
                "at": now,
                "result": copy.deepcopy(result),
            }
        return result

    def _resolve_domain(self, domain):
        exact_entries = [
            entry for entry in self.entries if entry["domain"] == domain
        ]
        if exact_entries:
            eligible = [entry for entry in exact_entries if self._eligible(entry)]
        else:
            eligible = self._wildcard_entries(domain)

        candidates = self._candidates_by_tenant(eligible)
        if not candidates:
            return {
                "status": "not_found",
                "tenant_id": None,
                "idp_id": None,
                "domain": domain,
                "candidates": [],
            }
        if len(candidates) > 1:
            return {
                "status": "conflict",
                "tenant_id": None,
                "idp_id": None,
                "domain": domain,
                "candidates": candidates,
            }

        selected = candidates[0]
        return {
            "status": "ok",
            "tenant_id": selected["tenant_id"],
            "idp_id": selected["idp_id"],
            "domain": domain,
            "candidates": [selected],
        }

    def _wildcard_entries(self, domain):
        if "." not in domain:
            return []

        suffix = domain.split(".", 1)[1]
        pattern = "*." + suffix
        return [
            entry
            for entry in self.entries
            if entry["wildcard"]
            and entry["domain"] == pattern
            and self._eligible(entry)
        ]

    def _eligible(self, entry):
        return entry["verified"] and entry["active"]

    def _candidates_by_tenant(self, entries):
        selected_by_tenant = {}
        for entry in entries:
            tenant_id = entry["tenant_id"]
            current = selected_by_tenant.get(tenant_id)
            if current is None or self._entry_sort_key(entry) < self._entry_sort_key(current):
                selected_by_tenant[tenant_id] = entry

        candidates = []
        for tenant_id in sorted(selected_by_tenant):
            entry = selected_by_tenant[tenant_id]
            candidates.append(
                {
                    "tenant_id": entry["tenant_id"],
                    "idp_id": entry["idp_id"],
                    "domain": entry["domain"],
                    "priority": entry["priority"],
                    "verified": entry["verified"],
                    "active": entry["active"],
                }
            )
        return candidates

    @staticmethod
    def _entry_sort_key(entry):
        return (-entry["priority"], entry["idp_id"])

    def _error(self, code):
        return {
            "status": "error",
            "error": code,
            "tenant_id": None,
            "idp_id": None,
            "domain": None,
            "candidates": [],
            "cached": False,
            "version": self.version,
        }

    def cache_info(self):
        return {
            "version": self.version,
            "size": len(self.cache),
            "cache_size": len(self.cache),
            "ttl": self.cache_ttl,
        }

    def snapshot(self):
        return {
            "version": self.version,
            "domains": copy.deepcopy(self.entries),
            "entries": copy.deepcopy(self.entries),
        }
