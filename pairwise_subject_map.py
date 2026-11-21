import hashlib
import hmac
import secrets
import threading
from collections.abc import Mapping
from urllib.parse import urlsplit


class PairwiseSubjectMap:
    def __init__(self, sector_ttl=3600, salt_grace=60):
        if sector_ttl < 0:
            raise ValueError("sector_ttl must not be negative")
        if salt_grace < 0:
            raise ValueError("salt_grace must not be negative")

        self.sector_ttl = sector_ttl
        self.salt_grace = salt_grace
        self.sectors = {}
        self.salts = {}
        self.mappings = {}
        self.events = []
        self._lock = threading.RLock()

    def register_sector(self, sector_id, document, now):
        sector_id = self._require_text(sector_id, "sector_id")
        validated = self._validate_sector_document(document)

        with self._lock:
            existing = self.sectors.get(sector_id)
            if existing and existing["origin"] != validated["origin"]:
                raise ValueError("sector document origin cannot change")

            self.sectors[sector_id] = {
                "sector_id": sector_id,
                "uri": validated["uri"],
                "redirect_uris": validated["redirect_uris"],
                "origin": validated["origin"],
                "registered_at": now,
                "expires_at": now + self.sector_ttl,
            }
            return self._sector_status_locked(sector_id, now)

    def subject_for(self, tenant_id, sector_id, local_subject, email=None,
                    public_sub=None, now=0):
        tenant_id = self._require_text(tenant_id, "tenant_id")
        sector_id = self._require_text(sector_id, "sector_id")
        local_subject = self._require_text(local_subject, "local_subject")
        normalized_email = self._normalize_email(email)

        with self._lock:
            self._require_sector_locked(sector_id, now)
            self._prune_salts_locked(now)

            key = (tenant_id, sector_id, local_subject)
            mapping = self.mappings.get(key)
            if mapping is not None:
                return self._mapping_result(mapping)

            salt_state = self._ensure_salt_locked(tenant_id, sector_id, now)
            version = salt_state["current"]
            salt = salt_state["versions"][version]["salt"]
            subject = self._derive_subject(
                salt, version, tenant_id, sector_id, local_subject,
                normalized_email,
            )

            mapping = {
                "tenant_id": tenant_id,
                "sector_id": sector_id,
                "local_subject": local_subject,
                "sub": subject,
                "salt_version": version,
                "created_at": now,
            }
            self.mappings[key] = mapping
            self.events.append({
                "type": "mint",
                "tenant_id": tenant_id,
                "sector_id": sector_id,
                "salt_version": version,
                "at": now,
            })
            return self._mapping_result(mapping)

    def rotate_salt(self, tenant_id, sector_id, now):
        tenant_id = self._require_text(tenant_id, "tenant_id")
        sector_id = self._require_text(sector_id, "sector_id")

        with self._lock:
            self._require_sector_locked(sector_id, now)
            self._prune_salts_locked(now)

            salt_state = self._ensure_salt_locked(tenant_id, sector_id, now)
            previous_version = salt_state["current"]
            previous = salt_state["versions"][previous_version]
            previous["retired_at"] = now
            previous["grace_until"] = now + self.salt_grace

            version = previous_version + 1
            salt_state["versions"][version] = {
                "salt": secrets.token_bytes(32),
                "created_at": now,
                "retired_at": None,
                "grace_until": None,
            }
            salt_state["current"] = version

            self.events.append({
                "type": "rotate",
                "tenant_id": tenant_id,
                "sector_id": sector_id,
                "salt_version": version,
                "at": now,
            })
            return {
                "tenant_id": tenant_id,
                "sector_id": sector_id,
                "version": version,
                "salt_version": version,
                "previous_version": previous_version,
                "grace_until": previous["grace_until"],
            }

    def lookup(self, tenant_id, sector_id, local_subject, now=0):
        tenant_id = self._require_text(tenant_id, "tenant_id")
        sector_id = self._require_text(sector_id, "sector_id")
        local_subject = self._require_text(local_subject, "local_subject")

        with self._lock:
            self._require_sector_locked(sector_id, now)
            mapping = self.mappings.get((tenant_id, sector_id, local_subject))
            if mapping is None:
                return None
            return self._mapping_result(mapping)

    def sector_status(self, sector_id, now):
        with self._lock:
            return self._sector_status_locked(sector_id, now)

    def audit(self):
        with self._lock:
            return [dict(entry) for entry in self.events]

    def stats(self):
        with self._lock:
            return {
                "mappings": len(self.mappings),
                "sectors": len(self.sectors),
                "tenants": len({key[0] for key in self.mappings}),
                "salt_sets": len(self.salts),
                "salt_versions": sum(
                    len(state["versions"]) for state in self.salts.values()
                ),
                "events": len(self.events),
            }

    @staticmethod
    def _require_text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _normalize_email(email):
        if email is None:
            return ""
        if not isinstance(email, str):
            raise ValueError("email must be a string")
        return email.strip().casefold()

    @classmethod
    def _validate_sector_document(cls, document):
        if not isinstance(document, Mapping):
            raise ValueError("sector document must be an object")

        uri = document.get("uri")
        redirect_uris = document.get("redirect_uris")
        if not isinstance(uri, str):
            raise ValueError("sector document uri must be a string")
        if not isinstance(redirect_uris, (list, tuple)) or not redirect_uris:
            raise ValueError("sector document redirect_uris must be a non-empty array")

        origin = cls._https_origin(uri)
        checked_redirects = []
        for redirect_uri in redirect_uris:
            if not isinstance(redirect_uri, str):
                raise ValueError("redirect URIs must be strings")
            if cls._https_origin(redirect_uri) != origin:
                raise ValueError("redirect URIs must share the sector document origin")
            checked_redirects.append(redirect_uri)

        return {
            "uri": uri,
            "redirect_uris": tuple(checked_redirects),
            "origin": origin,
        }

    @staticmethod
    def _https_origin(uri):
        if not isinstance(uri, str) or not uri:
            raise ValueError("URI must be a non-empty string")
        if any(character.isspace() for character in uri):
            raise ValueError("URI must not contain whitespace")

        try:
            parts = urlsplit(uri)
            hostname = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise ValueError("invalid URI") from exc

        hostname = hostname.lower().rstrip(".") if hostname else ""
        if parts.scheme.lower() != "https":
            raise ValueError("URI must use https")
        if not hostname:
            raise ValueError("URI must include a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("URI must not include credentials")
        if parts.fragment:
            raise ValueError("URI must not include a fragment")

        default_port = 443
        return ("https", hostname, port or default_port)

    def _require_sector_locked(self, sector_id, now):
        sector_id = self._require_text(sector_id, "sector_id")
        sector = self.sectors.get(sector_id)
        if sector is None:
            raise ValueError("sector is not registered")
        if now >= sector["expires_at"]:
            raise ValueError("sector document is stale")
        return sector

    def _sector_status_locked(self, sector_id, now):
        sector = self.sectors.get(sector_id)
        if sector is None:
            return {
                "sector_id": sector_id,
                "registered": False,
                "fresh": False,
                "stale": False,
                "valid": False,
                "uri": None,
                "redirect_uris": [],
                "registered_at": None,
                "expires_at": None,
            }

        fresh = now < sector["expires_at"]
        return {
            "sector_id": sector_id,
            "registered": True,
            "fresh": fresh,
            "stale": not fresh,
            "valid": fresh,
            "uri": sector["uri"],
            "redirect_uris": list(sector["redirect_uris"]),
            "registered_at": sector["registered_at"],
            "expires_at": sector["expires_at"],
        }

    def _ensure_salt_locked(self, tenant_id, sector_id, now):
        key = (tenant_id, sector_id)
        salt_state = self.salts.get(key)
        if salt_state is None:
            salt_state = {
                "current": 1,
                "versions": {
                    1: {
                        "salt": secrets.token_bytes(32),
                        "created_at": now,
                        "retired_at": None,
                        "grace_until": None,
                    }
                },
            }
            self.salts[key] = salt_state
        return salt_state

    def _prune_salts_locked(self, now):
        for salt_state in self.salts.values():
            current = salt_state["current"]
            expired = [
                version
                for version, metadata in salt_state["versions"].items()
                if version != current
                and metadata["grace_until"] is not None
                and now >= metadata["grace_until"]
            ]
            for version in expired:
                del salt_state["versions"][version]

    @staticmethod
    def _derive_subject(salt, version, tenant_id, sector_id, local_subject,
                        normalized_email):
        parts = (
            str(version),
            tenant_id,
            sector_id,
            local_subject,
            normalized_email,
        )
        material = b"".join(
            len(part.encode("utf-8")).to_bytes(4, "big") + part.encode("utf-8")
            for part in parts
        )
        return hmac.new(salt, material, hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _mapping_result(mapping):
        return {
            "tenant_id": mapping["tenant_id"],
            "sector_id": mapping["sector_id"],
            "sub": mapping["sub"],
            "salt_version": mapping["salt_version"],
            "created_at": mapping["created_at"],
        }
