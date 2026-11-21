import copy
import ipaddress
import re
import secrets


class SipError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RegisterGuard:
    def __init__(self, domains=(), min_refresh=60, bulk_limit=5, default_expires=3600, nonce_ttl=300):
        self.domains = {str(domain).strip().lower() for domain in domains if str(domain).strip()}
        self.min_refresh = min_refresh
        self.bulk_limit = bulk_limit
        self.default_expires = default_expires
        self.nonce_ttl = nonce_ttl
        self.rate_window = 1.0

        self._bindings = {}
        self._nonces = {}
        self._transactions = {}
        self._call_cseq = {}
        self._register_times = []
        self._registration_seq = 0
        self._clock = 0.0

    def issue_nonce(self, aor, now=0.0):
        self._validate_aor(aor)
        self._advance_clock(now)
        nonce = secrets.token_urlsafe(24)
        while nonce in self._nonces:
            nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = {
            "aor": aor,
            "issued_at": float(now),
            "used": False,
        }
        return nonce

    def challenge(self, aor, now=0.0):
        self._validate_aor(aor)
        nonce = self.issue_nonce(aor, now=now)
        return {
            "status": 401,
            "challenge": "Digest",
            "realm": self._realm_for(aor),
            "qop": "auth",
            "algorithm": "MD5",
            "nonce": nonce,
            "opaque": secrets.token_urlsafe(12),
        }

    def register(self, request, now=0.0):
        self._consume_rate_limit(1, now)
        return self._register(request, now)

    def register_bulk(self, requests, now=0.0):
        requests = list(requests)
        self._consume_rate_limit(len(requests), now)
        return [self._register(request, now) for request in requests]

    def bindings(self, aor, now=None):
        self._validate_aor(aor)
        if now is None:
            now = self._clock
        else:
            self._advance_clock(now)
        return [self._public_binding(binding) for binding in self._active_bindings(aor, now)]

    def targets(self, aor, now=None):
        return [binding["uri"] for binding in self.bindings(aor, now=now)]

    def primary_target(self, aor, now=None):
        targets = self.targets(aor, now=now)
        return targets[0] if targets else None

    def match_response(self, response):
        if not isinstance(response, dict):
            return None
        call_id = response.get("call_id")
        branch = response.get("branch")
        cseq = self._normalise_cseq(response.get("cseq"))
        if not call_id or not branch or cseq is None:
            return None

        key = (str(call_id).strip(), cseq, str(branch).strip())
        transaction = self._transactions.get(key)
        if transaction is None:
            return None

        transport = self._normalise_transport(response.get("transport"))
        if transport is not None:
            transaction["transport"] = transport
            history = transaction.setdefault("transport_history", [])
            if not history or history[-1] != transport:
                history.append(transport)

        matched = copy.deepcopy(transaction)
        matched["transport"] = transaction["transport"]
        if "status" in response:
            matched["status"] = response["status"]
            transaction["response_status"] = response["status"]
        return matched

    def snapshot(self):
        return {
            "bindings": {
                aor: [self._public_binding(binding) for binding in self._active_bindings(aor, self._clock)]
                for aor in self._bindings
            },
            "nonces": copy.deepcopy(self._nonces),
            "transactions": copy.deepcopy(self._transactions),
            "call_cseq": copy.deepcopy(self._call_cseq),
            "register_times": list(self._register_times),
        }

    def _register(self, request, now):
        if not isinstance(request, dict):
            raise SipError("invalid_request")

        self._advance_clock(now)
        if request.get("method") != "REGISTER":
            raise SipError("invalid_method")

        aor = request.get("aor")
        self._validate_aor(aor)

        call_id = request.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise SipError("invalid_call_id")
        call_id = call_id.strip()

        cseq = self._normalise_cseq(request.get("cseq"))
        if cseq is None or cseq <= 0:
            raise SipError("invalid_cseq")

        branch = request.get("branch")
        if not isinstance(branch, str) or not branch.strip():
            raise SipError("invalid_branch")
        branch = branch.strip()

        transport = self._normalise_transport(request.get("transport"))
        if transport is None:
            raise SipError("invalid_transport")

        source = self._parse_source(request.get("source"))
        if source is None:
            raise SipError("invalid_source")

        request_uri = self._normalise_uri(request.get("request_uri"))
        if request_uri is None:
            raise SipError("uri_mismatch")
        parsed_request_uri = self._parse_sip_uri(request_uri)
        if parsed_request_uri is None:
            raise SipError("uri_mismatch")
        self._validate_request_uri(aor, parsed_request_uri)

        self._check_replay(aor, call_id, cseq)

        authorization = request.get("authorization")
        if not isinstance(authorization, dict):
            raise SipError("missing_authorization")

        qop = authorization.get("qop")
        if not isinstance(qop, str) or qop.strip().lower() != "auth":
            raise SipError("unsupported_qop")

        auth_uri = self._normalise_uri(authorization.get("uri"))
        if auth_uri != request_uri:
            raise SipError("uri_mismatch")

        self._validate_authorization_identity(aor, authorization)
        nonce = self._validate_nonce(aor, authorization.get("nonce"), now)

        contacts = request.get("contacts")
        if contacts is None:
            contacts = []
        if not isinstance(contacts, (list, tuple)):
            raise SipError("invalid_contact")

        request_expires = self._normalise_expires(request.get("expires"))
        if request_expires is not None and request_expires < 0:
            raise SipError("invalid_expires")

        if request_expires is None:
            request_expires = self.default_expires

        if contacts:
            self._validate_refresh_interval(request_expires, contacts, now)
        elif request_expires > 0:
            self._validate_refresh_interval(request_expires, contacts, now)

        prepared = self._prepare_contacts(
            aor=aor,
            contacts=contacts,
            source=source,
            request_expires=request_expires,
            now=now,
        )
        self._validate_unregister_source(aor, prepared, source, now)

        self._commit_registration(
            aor=aor,
            call_id=call_id,
            cseq=cseq,
            branch=branch,
            transport=transport,
            source=source,
            request_uri=request_uri,
            request_expires=request_expires,
            prepared=prepared,
            nonce=nonce,
            now=now,
        )

        active = self._active_bindings(aor, now)
        result_expires = request_expires
        if result_expires is None:
            result_expires = max((binding["expires"] for binding in active), default=self.default_expires)
        return {
            "aor": aor,
            "contacts": [binding["uri"] for binding in active],
            "expires": result_expires,
            "call_id": call_id,
            "cseq": cseq,
            "branch": branch,
            "transport": transport,
        }

    def _prepare_contacts(self, aor, contacts, source, request_expires, now):
        aor_user, _ = self._split_aor(aor)
        prepared = []
        seen = set()

        for index, contact in enumerate(contacts):
            if isinstance(contact, str):
                contact = {"uri": contact}
            if not isinstance(contact, dict):
                raise SipError("invalid_contact")

            uri = self._normalise_uri(contact.get("uri"))
            if uri is None:
                raise SipError("invalid_contact")
            if uri == "*":
                if request_expires == 0 and len(contacts) == 1:
                    continue
                raise SipError("invalid_contact")
            parsed = self._parse_sip_uri(uri)
            if parsed is None:
                raise SipError("invalid_contact")

            scheme, user, host, _port = parsed
            if scheme not in ("sip", "sips"):
                raise SipError("invalid_contact")
            if not self._trusted_host(host):
                raise SipError("untrusted_domain")
            if user and aor_user and user != aor_user:
                raise SipError("identity_mismatch")

            contact_expires = self._normalise_expires(contact.get("expires"))
            if request_expires == 0:
                contact_expires = 0
            elif contact_expires is None:
                contact_expires = request_expires
            if contact_expires < 0:
                raise SipError("invalid_expires")

            q = contact.get("q", 1.0)
            if q is None:
                q = 1.0
            try:
                q = float(q)
            except (TypeError, ValueError):
                raise SipError("invalid_contact")
            if not 0.0 <= q <= 1.0:
                raise SipError("invalid_contact")

            self._validate_source_against_contact(source, host)

            if contact_expires > 0:
                self._validate_contact_refresh(aor, uri, now)

            if uri in seen:
                for item in prepared:
                    if item["uri"] == uri:
                        item.update({
                            "expires": contact_expires,
                            "q": q,
                            "index": index,
                        })
                continue
            seen.add(uri)
            prepared.append({
                "uri": uri,
                "host": host,
                "expires": contact_expires,
                "q": q,
                "index": index,
            })

        return prepared

    def _commit_registration(self, aor, call_id, cseq, branch, transport, source,
                             request_uri, request_expires, prepared, nonce, now):
        transaction_key = (call_id, cseq, branch)
        if transaction_key in self._transactions:
            raise SipError("replay")

        self._registration_seq += 1
        order = self._registration_seq
        now = float(now)

        if not prepared and request_expires == 0:
            self._bindings.pop(aor, None)
        else:
            current = self._bindings.setdefault(aor, {})
            for item in prepared:
                if item["expires"] == 0:
                    current.pop(item["uri"], None)
                    continue
                current[item["uri"]] = {
                    "uri": item["uri"],
                    "expires": item["expires"],
                    "q": item["q"],
                    "registered_at": now,
                    "updated_at": now,
                    "expires_at": now + item["expires"],
                    "source": source["text"],
                    "source_host": source["host"],
                    "transport": transport,
                    "request_call_id": call_id,
                    "request_cseq": cseq,
                    "order": (order, item["index"]),
                }
            if not current:
                self._bindings.pop(aor, None)

        self._nonces[nonce]["used"] = True
        self._nonces[nonce]["used_at"] = now
        self._call_cseq[(aor, call_id)] = cseq
        self._transactions[transaction_key] = {
            "call_id": call_id,
            "cseq": cseq,
            "branch": branch,
            "transport": transport,
            "transport_history": [transport],
            "aor": aor,
            "request_uri": request_uri,
            "source": source["text"],
            "created_at": now,
        }

    def _check_replay(self, aor, call_id, cseq):
        previous = self._call_cseq.get((aor, call_id))
        if previous is not None and cseq <= previous:
            raise SipError("replay")

    def _validate_nonce(self, aor, nonce, now):
        if not isinstance(nonce, str) or not nonce:
            raise SipError("stale_nonce")
        record = self._nonces.get(nonce)
        if record is None:
            raise SipError("stale_nonce")
        if record["used"] or record["aor"] != aor:
            raise SipError("stale_nonce")
        age = float(now) - record["issued_at"]
        if age < 0 or age > self.nonce_ttl:
            raise SipError("stale_nonce")
        return nonce

    def _validate_authorization_identity(self, aor, authorization):
        username = authorization.get("username")
        if not isinstance(username, str) or not username:
            raise SipError("identity_mismatch")

    def _validate_request_uri(self, aor, parsed_uri):
        _scheme, user, host, _port = parsed_uri
        aor_user, aor_domain = self._split_aor(aor)
        if not self._trusted_host(host):
            raise SipError("uri_mismatch")
        if host.lower() != aor_domain:
            raise SipError("uri_mismatch")
        if user is not None and user != aor_user:
            raise SipError("uri_mismatch")

    def _validate_refresh_interval(self, request_expires, contacts, now):
        if 0 < request_expires < self.min_refresh:
            raise SipError("interval_too_brief")
        for contact in contacts:
            expires = contact.get("expires") if isinstance(contact, dict) else None
            expires = self._normalise_expires(expires)
            if expires is not None and 0 < expires < self.min_refresh:
                raise SipError("interval_too_brief")

    def _validate_contact_refresh(self, aor, uri, now):
        binding = self._bindings.get(aor, {}).get(uri)
        if binding is None:
            return
        if now is None:
            return
        if float(now) - binding["updated_at"] < self.min_refresh:
            raise SipError("interval_too_brief")

    def _validate_unregister_source(self, aor, prepared, source, now):
        existing = self._bindings.get(aor, {})
        if not prepared:
            for binding in existing.values():
                self._validate_source_against_binding(source, binding)
            return

        for item in prepared:
            binding = existing.get(item["uri"])
            if binding is not None:
                self._validate_source_against_binding(source, binding)

    def _validate_source_against_contact(self, source, contact_host):
        try:
            contact_ip = ipaddress.ip_address(contact_host)
        except ValueError:
            if source["ip"] is None:
                if not self._trusted_host(source["host"]):
                    raise SipError("untrusted_source")
                if source["host"] != contact_host.lower():
                    raise SipError("source_mismatch")
            elif not self._is_internal_address(source["ip"]):
                raise SipError("source_mismatch")
            return

        if not self._is_internal_address(contact_ip):
            raise SipError("untrusted_domain")
        if source["ip"] is None:
            raise SipError("source_mismatch")
        if contact_ip.version != source["ip"].version:
            raise SipError("source_mismatch")
        prefix = 24 if contact_ip.version == 4 else 64
        if not contact_ip.is_global and not source["ip"].is_global:
            contact_network = ipaddress.ip_network("%s/%d" % (contact_ip, prefix), strict=False)
            if source["ip"] not in contact_network:
                raise SipError("source_mismatch")
        elif contact_ip != source["ip"]:
            raise SipError("source_mismatch")

    def _validate_source_against_binding(self, source, binding):
        allowed_hosts = {str(binding["source_host"]).lower()}
        parsed = self._parse_sip_uri(binding["uri"])
        if parsed is not None:
            allowed_hosts.add(parsed[2].lower())

        if source["host"] in allowed_hosts:
            return

        if source["ip"] is not None:
            for host in allowed_hosts:
                try:
                    if source["ip"] == ipaddress.ip_address(host):
                        return
                except ValueError:
                    continue
        raise SipError("source_mismatch")

    def _consume_rate_limit(self, amount, now):
        if amount < 0:
            raise SipError("rate_limited")
        now = float(now)
        cutoff = now - self.rate_window
        self._register_times = [stamp for stamp in self._register_times if stamp > cutoff]
        if len(self._register_times) + amount > self.bulk_limit:
            raise SipError("rate_limited")
        self._register_times.extend([now] * amount)

    def _active_bindings(self, aor, now):
        now = float(now)
        bindings = [
            binding
            for binding in self._bindings.get(aor, {}).values()
            if binding["expires_at"] > now
        ]
        bindings.sort(key=lambda binding: (-binding["order"][0], binding["order"][1]))
        return bindings

    def _public_binding(self, binding):
        return {
            "uri": binding["uri"],
            "expires": binding["expires"],
            "q": binding["q"],
            "registered_at": binding["registered_at"],
            "updated_at": binding["updated_at"],
            "expires_at": binding["expires_at"],
            "source": binding["source"],
        }

    def _validate_aor(self, aor):
        parsed = self._split_aor(aor)
        if parsed is None:
            raise SipError("invalid_aor")
        _user, domain = parsed
        if not self._trusted_host(domain):
            raise SipError("untrusted_domain")

    def _split_aor(self, aor):
        if not isinstance(aor, str) or aor.count("@") != 1:
            raise SipError("invalid_aor")
        user, domain = aor.split("@", 1)
        user = user.strip()
        domain = domain.strip().lower()
        if not user or not domain:
            raise SipError("invalid_aor")
        return user, domain

    def _trusted_host(self, host):
        host = str(host).strip().lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not self.domains:
                return True
            return any(host == domain or host.endswith("." + domain) for domain in self.domains)
        if not self.domains:
            return True
        return self._is_internal_address(address) or host in self.domains

    def _realm_for(self, aor):
        _user, domain = self._split_aor(aor)
        return domain

    @staticmethod
    def _is_internal_address(address):
        if address.version == 4:
            private_networks = (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("100.64.0.0/10"),
                ipaddress.ip_network("127.0.0.0/8"),
                ipaddress.ip_network("169.254.0.0/16"),
            )
        else:
            private_networks = (
                ipaddress.ip_network("fc00::/7"),
                ipaddress.ip_network("fe80::/10"),
                ipaddress.ip_network("::1/128"),
            )
        return any(address in network for network in private_networks)

    def _advance_clock(self, now):
        if now is None:
            return
        try:
            self._clock = max(self._clock, float(now))
        except (TypeError, ValueError):
            raise SipError("invalid_time")

    @staticmethod
    def _normalise_cseq(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        try:
            cseq = int(value)
        except (TypeError, ValueError):
            return None
        return cseq

    @staticmethod
    def _normalise_expires(value):
        if value is None:
            return None
        if isinstance(value, bool):
            raise SipError("invalid_expires")
        if isinstance(value, float) and not value.is_integer():
            raise SipError("invalid_expires")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SipError("invalid_expires")

    @staticmethod
    def _normalise_transport(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip().upper()

    @staticmethod
    def _normalise_uri(value):
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.startswith("<") and value.endswith(">"):
            value = value[1:-1].strip()
        return value or None

    @classmethod
    def _parse_source(cls, value):
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if value.startswith("["):
            match = re.match(r"^\[([^\]]+)\](?::\d+)?$", value)
            if not match:
                return None
            host = match.group(1)
        elif value.count(":") == 1:
            host, port = value.rsplit(":", 1)
            if not port.isdigit():
                return None
        else:
            host = value
        host = host.strip().lower()
        if not host:
            return None
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        return {"text": value, "host": host, "ip": ip}

    @staticmethod
    def _parse_sip_uri(uri):
        if not isinstance(uri, str):
            return None
        match = re.match(
            r"^(?P<scheme>sips?):(?:(?P<user>[^@;:>]+)@)?"
            r"(?P<host>\[[^\]]+\]|[^;:>]+)(?::(?P<port>\d+))?",
            uri.strip(),
            re.IGNORECASE,
        )
        if not match:
            return None
        scheme = match.group("scheme").lower()
        user = match.group("user")
        host = match.group("host").strip().lower()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        port = match.group("port")
        return scheme, user, host, int(port) if port else None
