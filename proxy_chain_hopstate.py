import base64
import re
import threading
from urllib.parse import urlsplit, urlunsplit


class ProxyChainError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})

_UNTRUSTED_TRACE_HEADERS = frozenset({
    "traceparent",
    "tracestate",
    "baggage",
})

_VIA_VERSION = re.compile(r"^(?:HTTP/)?\d(?:\.\d)?$", re.IGNORECASE)
_VIA_COMMENT = re.compile(r"\([^()]*\)")


def _header_items(headers):
    if headers is None:
        return []
    if hasattr(headers, "items"):
        return list(headers.items())
    return list(headers)


def _header_values(headers, name):
    wanted = name.lower()
    values = []
    for key, value in _header_items(headers):
        if str(key).strip().lower() != wanted:
            continue
        if isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
        else:
            values.append(str(value))
    return values


def _copy_headers_without(headers, excluded):
    result = {}
    names_by_lower = {}
    for key, value in _header_items(headers):
        lowered = str(key).strip().lower()
        if lowered in excluded:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        else:
            value = str(value)
        if lowered in names_by_lower:
            current = names_by_lower[lowered]
            result[current] = "{}, {}".format(result[current], value)
        else:
            name = str(key)
            names_by_lower[lowered] = name
            result[name] = value
    return result


def _split_header_tokens(value):
    in_quotes = False
    depth = 0
    escaped = False
    start = 0
    tokens = []
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_quotes:
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            tokens.append(value[start:index].strip())
            start = index + 1
    tokens.append(value[start:].strip())
    return [token for token in tokens if token]


def _via_endpoint(entry):
    entry = _VIA_COMMENT.sub("", entry).strip()
    parts = entry.split()
    if len(parts) > 1 and _VIA_VERSION.match(parts[0]):
        entry = parts[1]
    else:
        entry = parts[0] if parts else ""
    entry = entry.strip().strip('"').lower()
    if not entry:
        return "", None
    try:
        parsed = urlsplit("//" + entry)
        hostname = parsed.hostname
        port = parsed.port
    except (ValueError, TypeError):
        hostname = None
        port = None
    if hostname:
        return hostname.lower(), port
    return entry, None


def _hop_identities(hop, self_token):
    identities = []
    for key in ("via", "host", "id"):
        value = hop.get(key)
        if value:
            identities.append(str(value))
    if self_token:
        identities.append(str(self_token))
    expanded = []
    for identity in identities:
        expanded.append(identity)
        hostname, _ = _via_endpoint(identity)
        if hostname:
            expanded.append(hostname)
    return expanded


def _via_contains_hop(via_value, hop, self_token):
    aliases = {alias.lower() for alias in _hop_identities(hop, self_token)}
    alias_hosts = {_via_endpoint(alias)[0] for alias in aliases}
    alias_hosts.discard("")
    for entry in _split_header_tokens(via_value):
        endpoint, _ = _via_endpoint(entry)
        if endpoint and (endpoint in aliases or endpoint in alias_hosts):
            return True
    return False


def _basic_proxy_authorization(credentials):
    user = credentials.get("user", credentials.get("username"))
    password = credentials.get("password")
    if user is None or password is None:
        raise ProxyChainError("missing_credentials")
    raw = "{}:{}".format(user, password).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _decode_chunked(body):
    decoded = bytearray()
    position = 0
    total = len(body)
    while True:
        line_end = body.find(b"\r\n", position)
        if line_end < 0:
            raise ProxyChainError("invalid_chunked", "missing_chunk_size")
        size_field = body[position:line_end].split(b";", 1)[0].strip()
        if not size_field:
            raise ProxyChainError("invalid_chunked", "missing_chunk_size")
        try:
            size = int(size_field, 16)
        except ValueError:
            raise ProxyChainError("invalid_chunked", "invalid_chunk_size") from None
        if size < 0:
            raise ProxyChainError("invalid_chunked", "invalid_chunk_size")
        position = line_end + 2
        if size == 0:
            while True:
                trailer_end = body.find(b"\r\n", position)
                if trailer_end < 0:
                    raise ProxyChainError("invalid_chunked", "missing_trailer_end")
                trailer = body[position:trailer_end]
                position = trailer_end + 2
                if not trailer:
                    break
            if position != total:
                raise ProxyChainError("invalid_chunked", "trailing_bytes")
            return bytes(decoded)
        chunk_end = position + size
        if chunk_end + 2 > total:
            raise ProxyChainError("invalid_chunked", "truncated_chunk")
        decoded.extend(body[position:chunk_end])
        position = chunk_end
        if body[position:position + 2] != b"\r\n":
            raise ProxyChainError("invalid_chunked", "missing_chunk_terminator")
        position += 2


class HopState:
    def __init__(self, hops=(), self_token="chain-local", max_auth_retries=2):
        self.hops = [dict(hop) for hop in hops]
        self.self_token = self_token
        self.max_auth_retries = int(max_auth_retries)
        if self.max_auth_retries < 0:
            raise ValueError("max_auth_retries must be non-negative")

        self._hops_by_id = {}
        for hop in self.hops:
            hop_id = hop.get("id")
            if not hop_id:
                raise ProxyChainError("invalid_hop", "missing_id")
            if hop_id in self._hops_by_id:
                raise ProxyChainError("invalid_hop", "duplicate_id")
            self._hops_by_id[hop_id] = hop

        self._auth_attempts = {hop_id: 0 for hop_id in self._hops_by_id}
        self._connections = {}
        self._connection_lock = threading.RLock()

    def _hop(self, proxy_id):
        try:
            return self._hops_by_id[proxy_id]
        except KeyError:
            raise ProxyChainError("unknown_proxy") from None

    def sanitize_headers(self, headers, hop_id):
        hop = self._hop(hop_id)
        connection_named = set()
        for name in ("connection", "proxy-connection"):
            for value in _header_values(headers, name):
                connection_named.update(
                    token.lower() for token in _split_header_tokens(value)
                )

        excluded = set(_HOP_BY_HOP_HEADERS)
        excluded.update(connection_named)
        excluded.update(_UNTRUSTED_TRACE_HEADERS)
        result = _copy_headers_without(headers, excluded)

        existing_via = ", ".join(_header_values(result, "via"))
        if existing_via and _via_contains_hop(existing_via, hop, self.self_token):
            raise ProxyChainError("loop")

        received_by = hop.get("via") or hop.get("host") or hop.get("id")
        received_by = str(received_by)
        result["Via"] = (
            "{}, 1.1 {}".format(existing_via, received_by)
            if existing_via
            else "1.1 {}".format(received_by)
        )
        result["Proxy-Authorization"] = _basic_proxy_authorization(
            self.auth_for(hop_id)
        )
        return result

    def auth_for(self, proxy_id):
        hop = self._hop(proxy_id)
        credentials = hop.get("credentials") or {}
        return dict(credentials)

    def rewrite_target(self, request, target):
        target_parts = urlsplit(str(target))
        if not target_parts.scheme or not target_parts.netloc:
            raise ProxyChainError("invalid_target")

        request_uri = str(request.get("uri", ""))
        original_parts = urlsplit(request_uri)
        if original_parts.scheme and original_parts.netloc:
            path = original_parts.path or "/"
            query = original_parts.query
        else:
            path = original_parts.path or request_uri or "/"
            query = original_parts.query

        rewritten = urlunsplit(
            (target_parts.scheme, target_parts.netloc, path, query, "")
        )
        rewritten_request = dict(request)
        rewritten_request["uri"] = rewritten

        headers = _copy_headers_without(
            request.get("headers"), {"host"}
        )
        headers["Host"] = target_parts.netloc
        rewritten_request["headers"] = headers
        return rewritten_request

    def decode_body(self, body, headers):
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif body is None:
            data = b""
        else:
            data = bytes(body)

        transfer_codings = []
        for value in _header_values(headers, "transfer-encoding"):
            transfer_codings.extend(
                token.strip().lower() for token in _split_header_tokens(value)
            )
        if "chunked" in transfer_codings:
            data = _decode_chunked(data)

        result_headers = _copy_headers_without(
            headers, {"transfer-encoding", "content-length"}
        )
        result_headers["Content-Length"] = str(len(data))
        return data, result_headers

    def tunnel_state(self, events):
        state = {
            "to_server": 0,
            "to_client": 0,
            "client_closed": False,
            "server_closed": False,
            "closed": False,
        }
        for event in events:
            side = str(event.get("side", "")).lower()
            kind = str(event.get("event", "")).lower().replace("-", "_")
            if side not in ("client", "server"):
                raise ProxyChainError("invalid_tunnel_event", "bad_side")
            if kind == "data":
                if state["closed"] or state[side + "_closed"]:
                    continue
                payload = event.get("payload", b"")
                if isinstance(payload, str):
                    size = len(payload.encode("utf-8"))
                else:
                    size = len(payload)
                direction = "to_server" if side == "client" else "to_client"
                state[direction] += size
            elif kind == "half_close":
                state[side + "_closed"] = True
                state["closed"] = (
                    state["client_closed"] and state["server_closed"]
                )
            elif kind == "close":
                state["client_closed"] = True
                state["server_closed"] = True
                state["closed"] = True
            else:
                raise ProxyChainError("invalid_tunnel_event", "bad_event")
        return state

    def effective_idle_timeout(self):
        timeouts = []
        for hop in self.hops:
            timeout = hop.get("idle_timeout")
            if timeout is None:
                continue
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                raise ProxyChainError("invalid_timeout") from None
            timeouts.append(timeout)
        return min(timeouts) if timeouts else None

    def connection_key(self, hop_id, target):
        self._hop(hop_id)
        return hop_id, str(target)

    def acquire(self, hop_id, target):
        key = self.connection_key(hop_id, target)
        with self._connection_lock:
            connection = self._connections.get(key)
            if connection is None:
                connection = {
                    "key": key,
                    "hop_id": hop_id,
                    "target": str(target),
                    "closed": False,
                }
                self._connections[key] = connection
            return connection

    def on_407(self, proxy_id):
        self._hop(proxy_id)
        attempts = self._auth_attempts[proxy_id] + 1
        if attempts > self.max_auth_retries:
            raise ProxyChainError("auth_retries")
        self._auth_attempts[proxy_id] = attempts
        return {"action": "retry", "attempts": attempts}

    def match_responses(self, requests, responses):
        request_ids = []
        seen_requests = set()
        for request in requests:
            request_id = request.get("id")
            if request_id is None:
                raise ProxyChainError("invalid_request", "missing_id")
            if request_id in seen_requests:
                raise ProxyChainError("duplicate_request")
            seen_requests.add(request_id)
            request_ids.append(request_id)

        responses_by_request = {}
        for response in responses:
            request_id = response.get("request_id")
            if request_id is None:
                raise ProxyChainError("invalid_response", "missing_request_id")
            if request_id in responses_by_request:
                raise ProxyChainError("duplicate_response")
            if request_id not in seen_requests:
                raise ProxyChainError("unexpected_response")
            responses_by_request[request_id] = response

        missing = [request_id for request_id in request_ids
                   if request_id not in responses_by_request]
        if missing:
            raise ProxyChainError("missing_response", {"request_ids": missing})

        return {
            request_id: responses_by_request[request_id]
            for request_id in request_ids
        }
