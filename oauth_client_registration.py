import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
from copy import deepcopy
from urllib.parse import urlsplit


DEFAULT_SCOPES = ["openid", "files.read", "files.write"]
DEFAULT_GRANTS = ["authorization_code", "client_credentials", "refresh_token"]
DEFAULT_AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post"}
LOOPBACK_HOSTS = {"localhost", "localhost.localdomain"}
INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".home.arpa",
)
URI_FIELDS = {
    "client_uri",
    "logo_uri",
    "policy_uri",
    "tos_uri",
    "jwks_uri",
    "sector_identifier_uri",
    "initiate_login_uri",
}
URI_LIST_FIELDS = {"redirect_uris", "request_uris"}

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600


def _token():
    return secrets.token_hex(16)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _constant_time_equal(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _is_uri_field(field):
    return (
        field in URI_FIELDS
        or field in URI_LIST_FIELDS
        or field.endswith(("_uri", "_uris", "_url", "_urls"))
    )


def _is_uri_list_field(field):
    return field in URI_LIST_FIELDS or field.endswith(("_uris", "_urls"))


class DynamicClientRegistry:
    def __init__(self, allowed_scopes=None, allowed_grants=None, trusted_signature="sig-ok"):
        self.allowed_scopes = list(DEFAULT_SCOPES if allowed_scopes is None else allowed_scopes)
        self.allowed_grants = list(DEFAULT_GRANTS if allowed_grants is None else allowed_grants)
        self.trusted_signature = trusted_signature

        # All access to the in-memory registry is serialized. This is important
        # for the software_id based idempotency check: checking and inserting must
        # be one atomic operation.
        self._lock = threading.RLock()
        self._clients = {}
        self._clients_by_software_id = {}
        self._tokens = {}
        self._audit_events = []

    def _record(self, event, now, **details):
        entry = {"event": event, "at": now}
        entry.update(details)
        self._audit_events.append(entry)

    @staticmethod
    def _is_loopback_host(host):
        host = host.lower().rstrip(".")
        if host in LOOPBACK_HOSTS or host.endswith(".localhost"):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
            except (OSError, ValueError):
                return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback

    @staticmethod
    def _parse_ip(host):
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            pass

        # inet_aton also recognizes legacy IPv4 spellings such as "0177.0.0.1".
        # They must not be treated as harmless DNS names.
        try:
            return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
        except (OSError, ValueError):
            return None

    def _validate_host(self, host, allow_loopback):
        if not host:
            raise ValueError("URL must contain a host")

        host = host.lower().rstrip(".")
        if not host or "*" in host or "%" in host:
            raise ValueError("invalid or wildcard host")

        address = self._parse_ip(host)
        if address is not None:
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if address.is_loopback:
                if allow_loopback:
                    return host
                raise ValueError("loopback hosts are not allowed in this field")
            if not address.is_global:
                raise ValueError("private or reserved addresses are not allowed")
            return host

        if host in LOOPBACK_HOSTS or host.endswith(".localhost"):
            if allow_loopback:
                return host
            raise ValueError("loopback hosts are not allowed in this field")

        if host.endswith(INTERNAL_SUFFIXES):
            raise ValueError("internal host names are not allowed")

        # A bare host is usually an intranet name. Public registrable names
        # contain at least one dot.
        if "." not in host:
            raise ValueError("host is not a public domain name")
        if any(not label for label in host.split(".")):
            raise ValueError("host name contains an empty label")

        # Reject malformed all-numeric host names that inet_aton could not parse.
        labels = host.split(".")
        if all(label.isdigit() or label.lower().startswith("0x") for label in labels):
            raise ValueError("malformed numeric host is not allowed")

        return host

    def _normalize_uri(self, uri, kind):
        if not isinstance(uri, str) or not uri:
            raise ValueError("URI must be a non-empty string")
        if "*" in uri:
            raise ValueError("wildcard URIs are not allowed")
        if "#" in uri:
            raise ValueError("URI fragments are not allowed")
        if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in uri):
            raise ValueError("URI contains whitespace or control characters")
        if "\\" in uri:
            raise ValueError("backslashes are not allowed in URIs")

        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid URI") from exc

        if not parsed.scheme or not parsed.netloc:
            raise ValueError("URI must be absolute")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("user information is not allowed in URIs")

        scheme = parsed.scheme.lower()
        host = parsed.hostname
        allow_loopback = kind == "redirect"

        if scheme not in {"http", "https"}:
            raise ValueError("unsupported URI scheme")
        if scheme == "http" and not allow_loopback:
            raise ValueError("only HTTPS is allowed for this field")
        if scheme == "http" and not self._is_loopback_host(host or ""):
            raise ValueError("plain HTTP is only allowed for loopback redirects")
        if port == 0:
            raise ValueError("invalid port")

        host = self._validate_host(host or "", allow_loopback)

        default_port = 443 if scheme == "https" else 80
        if port == default_port:
            port = None

        display_host = "[{}]".format(host) if ":" in host else host
        netloc = display_host if port is None else "{}:{}".format(display_host, port)

        normalized = "{}://{}{}".format(scheme, netloc, parsed.path)
        if parsed.query or "?" in uri:
            normalized += "?" + parsed.query
        return normalized

    def normalize_redirect_uri(self, uri):
        return self._normalize_uri(uri, "redirect")

    def _normalize_non_redirect_uri(self, uri):
        return self._normalize_uri(uri, "non-redirect")

    @staticmethod
    def _string_list(value, field, allow_empty):
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise ValueError("{} must be a list".format(field))
        result = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError("{} contains an invalid value".format(field))
            result.append(item)
        if not result and not allow_empty:
            raise ValueError("{} must not be empty".format(field))
        return result

    @staticmethod
    def _deduplicate(values):
        seen = set()
        result = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _validate_software_statement(self, statement, now):
        if not isinstance(statement, dict):
            raise ValueError("software_statement is required")

        signature = statement.get("signature")
        if not _constant_time_equal(signature, self.trusted_signature):
            raise ValueError("software_statement signature is invalid")

        expires_at = statement.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise ValueError("software_statement expiry is invalid")
        if expires_at <= now:
            raise ValueError("software_statement has expired")

        not_before = statement.get("not_before")
        if not_before is not None:
            if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
                raise ValueError("software_statement not_before is invalid")
            if now < not_before:
                raise ValueError("software_statement is not yet valid")

    def _validate_claim(self, claim, now):
        if not isinstance(claim, dict):
            raise ValueError("claim must be an object")

        software_id = claim.get("software_id")
        client_name = claim.get("client_name")
        if not isinstance(software_id, str) or not software_id.strip():
            raise ValueError("software_id is required")
        if not isinstance(client_name, str) or not client_name.strip():
            raise ValueError("client_name is required")

        self._validate_software_statement(claim.get("software_statement"), now)

        raw_redirects = self._string_list(
            claim.get("redirect_uris"), "redirect_uris", allow_empty=True
        )
        redirect_uris = self._deduplicate(
            [self.normalize_redirect_uri(uri) for uri in raw_redirects]
        )

        grants = self._deduplicate(
            self._string_list(claim.get("grant_types"), "grant_types", allow_empty=False)
        )
        for grant in grants:
            if grant not in self.allowed_grants:
                raise ValueError("grant type is not allowed: {}".format(grant))

        scopes = self._deduplicate(
            self._string_list(claim.get("scopes"), "scopes", allow_empty=False)
        )
        for scope in scopes:
            if scope not in self.allowed_scopes:
                raise ValueError("scope is not allowed: {}".format(scope))

        auth_method = claim.get("token_endpoint_auth_method", "client_secret_basic")
        if auth_method not in DEFAULT_AUTH_METHODS:
            raise ValueError("unsupported token_endpoint_auth_method")

        if "authorization_code" in grants and not redirect_uris:
            raise ValueError("authorization_code requires a redirect URI")
        if "client_credentials" in grants and not scopes:
            raise ValueError("client_credentials requires at least one scope")
        if auth_method == "none" and "client_credentials" in grants:
            raise ValueError("public clients cannot use client_credentials")

        result = {key: deepcopy(value) for key, value in claim.items()}
        result["software_id"] = software_id.strip()
        result["client_name"] = client_name.strip()
        result["redirect_uris"] = redirect_uris
        result["grant_types"] = grants
        result["scopes"] = scopes
        result["token_endpoint_auth_method"] = auth_method

        # Validate every URI-bearing registration field, including extension
        # fields, instead of only the well-known ones.
        for field, value in list(result.items()):
            if field == "redirect_uris":
                continue
            if _is_uri_list_field(field):
                values = self._string_list(value, field, allow_empty=True)
                result[field] = self._deduplicate(
                    [self._normalize_non_redirect_uri(item) for item in values]
                )
            elif _is_uri_field(field):
                result[field] = self._normalize_non_redirect_uri(value)

        return result

    @staticmethod
    def _public_result(
        client,
        idempotent=False,
        include_credentials=False,
        include_null_secret=False,
        client_secret=None,
        registration_access_token=None,
    ):
        result = {
            "client_id": client["client_id"],
            "client_name": client["client_name"],
            "software_id": client["software_id"],
            "redirect_uris": list(client["redirect_uris"]),
            "token_endpoint_auth_method": client["token_endpoint_auth_method"],
            "grant_types": list(client["grant_types"]),
            "scopes": list(client["scopes"]),
            "idempotent": idempotent,
        }
        for field, value in client.items():
            if field == "redirect_uris":
                continue
            if _is_uri_field(field):
                result[field] = deepcopy(value)
        if include_credentials:
            result["client_secret"] = client_secret
            result["registration_access_token"] = registration_access_token
        elif include_null_secret:
            result["client_secret"] = None
            result["registration_access_token"] = None
        return result

    def _require_client(self, client_id, registration_token):
        if not isinstance(client_id, str) or not isinstance(registration_token, str):
            raise ValueError("invalid client credentials")
        client = self._clients.get(client_id)
        if client is None:
            raise ValueError("unknown client")
        if not _constant_time_equal(
            _digest(registration_token), client["registration_token_digest"]
        ):
            raise ValueError("invalid registration access token")
        return client

    def register(self, claim, now):
        validated = self._validate_claim(claim, now)
        software_id = validated["software_id"]

        with self._lock:
            existing = self._clients_by_software_id.get(software_id)
            if existing is not None:
                client = self._clients.get(existing)
                if client is not None:
                    self._record("client_registration_idempotent", now, client_id=existing)
                    return self._public_result(
                        client, idempotent=True, include_null_secret=True
                    )

            client_id = _token()
            while client_id in self._clients:
                client_id = _token()

            auth_method = validated["token_endpoint_auth_method"]
            raw_secret = None if auth_method == "none" else _token()
            registration_token = _token()

            client = {
                "client_id": client_id,
                "software_id": software_id,
                "client_name": validated["client_name"],
                "redirect_uris": validated["redirect_uris"],
                "token_endpoint_auth_method": auth_method,
                "grant_types": validated["grant_types"],
                "scopes": validated["scopes"],
                "created_at": now,
                "updated_at": now,
                "client_secret_hash": (
                    None if raw_secret is None else _digest(raw_secret)
                ),
                "registration_token_digest": _digest(registration_token),
            }
            for field, value in validated.items():
                if field == "redirect_uris":
                    continue
                if _is_uri_field(field):
                    client[field] = deepcopy(value)

            self._clients[client_id] = client
            self._clients_by_software_id[software_id] = client_id
            self._record("client_registered", now, client_id=client_id)
            return self._public_result(
                client,
                include_credentials=True,
                client_secret=raw_secret,
                registration_access_token=registration_token,
            )

    def describe(self, client_id, registration_token):
        with self._lock:
            client = self._require_client(client_id, registration_token)
            result = self._public_result(client)
            result["created_at"] = client["created_at"]
            result["updated_at"] = client["updated_at"]
            return result

    def rotate_credential(self, client_id, registration_token, now):
        with self._lock:
            client = self._require_client(client_id, registration_token)

            registration_token = _token()
            client["registration_token_digest"] = _digest(registration_token)

            if client["token_endpoint_auth_method"] == "none":
                client["client_secret_hash"] = None
                client_secret = None
            else:
                client_secret = _token()
                client["client_secret_hash"] = _digest(client_secret)

            client["updated_at"] = now
            self._record("client_credential_rotated", now, client_id=client_id)
            return self._public_result(
                client,
                include_credentials=True,
                client_secret=client_secret,
                registration_access_token=registration_token,
            )

    def delete(self, client_id, registration_token, now):
        with self._lock:
            client = self._require_client(client_id, registration_token)

            for token_record in self._tokens.values():
                if token_record["client_id"] == client_id:
                    token_record["revoked"] = True

            self._clients.pop(client_id, None)
            if self._clients_by_software_id.get(client["software_id"]) == client_id:
                del self._clients_by_software_id[client["software_id"]]

            self._record("client_deleted", now, client_id=client_id)
            return {"client_id": client_id, "deleted": True}

    def _issue_tokens(self, client, grant_type, scopes, now):
        access_token = _token()
        self._tokens[_digest(access_token)] = {
            "client_id": client["client_id"],
            "kind": "access",
            "issued_at": now,
            "expires_at": now + ACCESS_TOKEN_TTL,
            "revoked": False,
            "scopes": list(scopes),
        }

        refresh_token = None
        if grant_type == "authorization_code":
            refresh_token = _token()
            self._tokens[_digest(refresh_token)] = {
                "client_id": client["client_id"],
                "kind": "refresh",
                "issued_at": now,
                "expires_at": now + REFRESH_TOKEN_TTL,
                "revoked": False,
                "scopes": list(scopes),
                "grant_type": grant_type,
            }

        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": " ".join(scopes),
            "refresh_token": refresh_token,
        }

    def issue_token(self, client_id, grant_type, scopes, now):
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                raise ValueError("unknown client")
            if grant_type not in self.allowed_grants:
                raise ValueError("grant type is not allowed")
            if grant_type not in client["grant_types"]:
                raise ValueError("grant type was not registered for this client")
            if client["token_endpoint_auth_method"] == "none" and grant_type == "client_credentials":
                raise ValueError("public clients cannot use client_credentials")

            requested = self._string_list(scopes, "scopes", allow_empty=False)
            for scope in requested:
                if scope not in client["scopes"] or scope not in self.allowed_scopes:
                    raise ValueError("requested scope is not allowed")

            result = self._issue_tokens(client, grant_type, requested, now)
            self._record("token_issued", now, client_id=client_id, grant_type=grant_type)
            return result

    def refresh(self, refresh_token, now):
        with self._lock:
            if not isinstance(refresh_token, str) or not refresh_token:
                raise ValueError("invalid refresh token")

            digest = _digest(refresh_token)
            record = self._tokens.get(digest)
            if (
                record is None
                or record.get("kind") != "refresh"
                or record.get("revoked")
                or record["expires_at"] <= now
            ):
                raise ValueError("invalid or expired refresh token")

            client = self._clients.get(record["client_id"])
            if client is None:
                record["revoked"] = True
                raise ValueError("client no longer exists")

            # Refresh tokens are single-use. Rotating them prevents replay and
            # makes deletion/revocation effective for every earlier credential.
            record["revoked"] = True
            result = self._issue_tokens(
                client, "authorization_code", record["scopes"], now
            )
            self._record("token_refreshed", now, client_id=client["client_id"])
            return result

    def audit(self):
        with self._lock:
            return deepcopy(self._audit_events)

    def stats(self):
        with self._lock:
            active_refresh_tokens = sum(
                1
                for record in self._tokens.values()
                if record.get("kind") == "refresh" and not record.get("revoked")
            )
            return {
                "clients": len(self._clients),
                "audit_events": len(self._audit_events),
                "active_refresh_tokens": active_refresh_tokens,
            }
