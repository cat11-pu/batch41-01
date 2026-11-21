import base64

import pytest

import proxy_chain_hopstate as pc

HOPS = [
    {"id": "p1", "via": "edge1.example", "host": "edge1.example:8080",
     "credentials": {"user": "alice", "password": "s1"}, "idle_timeout": 30},
    {"id": "p2", "via": "edge2.example", "host": "edge2.example:8080",
     "credentials": {"user": "bob", "password": "s2"}, "idle_timeout": 10},
    {"id": "p3", "via": "edge3.example", "host": "edge3.example:8080",
     "credentials": {"user": "carol", "password": "s3"}, "idle_timeout": 20},
]


def guard(**kwargs):
    return pc.HopState(HOPS, **kwargs)


def basic_value(header):
    return base64.b64decode(header.split(" ", 1)[1]).decode()


def test_proxy_authorization_is_not_forwarded():
    out = guard().sanitize_headers(
        {"Proxy-Authorization": "Basic ZXZpbDpzZWNyZXQ=", "X-Test": "1"}, "p1")
    assert out["X-Test"] == "1"
    assert out["Proxy-Authorization"].startswith("Basic ")
    assert basic_value(out["Proxy-Authorization"]) == "alice:s1"


def test_connection_listed_headers_are_stripped():
    out = guard().sanitize_headers(
        {"Connection": "X-Debug", "X-Debug": "1", "X-Keep": "2"}, "p1")
    assert "X-Debug" not in out
    assert out["X-Keep"] == "2"
    assert "Connection" not in out


def test_keep_alive_and_te_are_stripped():
    out = guard().sanitize_headers(
        {"Keep-Alive": "timeout=5", "TE": "trailers", "Transfer-Encoding": "chunked"}, "p1")
    assert "Keep-Alive" not in out
    assert "TE" not in out
    assert "Transfer-Encoding" not in out


def test_via_receives_this_hop():
    out = guard().sanitize_headers({"Via": "1.1 cdn.example"}, "p1")
    assert "cdn.example" in out["Via"]
    assert "edge1.example" in out["Via"]


def test_via_loop_is_detected():
    with pytest.raises(pc.ProxyChainError) as exc:
        guard().sanitize_headers({"Via": "1.1 edge1.example"}, "p1")
    assert exc.value.code == "loop"


def test_traceparent_is_stripped():
    out = guard().sanitize_headers(
        {"traceparent": "00-abc-def-01", "tracestate": "vendor=1", "X-Ok": "1"}, "p1")
    assert "traceparent" not in out
    assert "tracestate" not in out
    assert out["X-Ok"] == "1"


def test_credentials_are_keyed_by_proxy():
    g = guard()
    assert g.auth_for("p1")["user"] == "alice"
    assert g.auth_for("p2")["user"] == "bob"
    with pytest.raises(pc.ProxyChainError) as exc:
        g.auth_for("p9")
    assert exc.value.code == "unknown_proxy"


def test_failover_does_not_reuse_previous_credentials():
    g = guard()
    g.on_407("p1")
    assert g.auth_for("p2")["user"] == "bob"
    assert g.auth_for("p2")["password"] == "s2"
    assert g.auth_for("p1")["user"] == "alice"


def test_target_rewrite_updates_host_and_uri():
    g = guard()
    request = {"method": "GET", "uri": "http://origin.example/a/b?x=1",
               "headers": {"Host": "origin.example"}}
    out = g.rewrite_target(request, "http://edge2.example:8080")
    assert out["uri"] == "http://edge2.example:8080/a/b?x=1"
    assert out["headers"]["Host"] == "edge2.example:8080"
    assert request["uri"] == "http://origin.example/a/b?x=1"


def test_chunked_body_is_decoded_once():
    g = guard()
    payload, headers = g.decode_body(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n",
                                     {"Transfer-Encoding": "chunked"})
    assert payload == b"Wikipedia"
    assert "Transfer-Encoding" not in headers
    assert headers["Content-Length"] == "9"
    again, headers2 = g.decode_body(payload, headers)
    assert again == b"Wikipedia"
    assert "Transfer-Encoding" not in headers2


def test_tunnel_half_close_keeps_other_direction():
    g = guard()
    state = g.tunnel_state([
        {"side": "client", "event": "data", "payload": b"xy"},
        {"side": "client", "event": "half_close"},
        {"side": "client", "event": "data", "payload": b"zz"},
        {"side": "server", "event": "data", "payload": b"abc"},
        {"side": "server", "event": "half_close"},
    ])
    assert state["to_server"] == 2
    assert state["to_client"] == 3
    assert state["client_closed"] is True
    assert state["server_closed"] is True
    assert state["closed"] is True


def test_idle_timeout_is_minimum_of_hops():
    assert guard().effective_idle_timeout() == 10


def test_connection_pool_is_isolated_by_egress():
    g = guard()
    first = g.acquire("p1", "https://origin.example")
    reused = g.acquire("p1", "https://origin.example")
    other = g.acquire("p2", "https://origin.example")
    assert first is reused
    assert first is not other
    assert g.connection_key("p1", "https://origin.example") != g.connection_key("p2", "https://origin.example")


def test_auth_retry_limit_is_enforced():
    g = guard(max_auth_retries=2)
    assert g.on_407("p1")["action"] == "retry"
    assert g.on_407("p1")["attempts"] == 2
    with pytest.raises(pc.ProxyChainError) as exc:
        g.on_407("p1")
    assert exc.value.code == "auth_retries"


def test_responses_are_matched_by_request_id():
    g = guard()
    requests = [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]
    responses = [
        {"request_id": "r3", "body": b"3"},
        {"request_id": "r1", "body": b"1"},
        {"request_id": "r2", "body": b"2"},
    ]
    out = g.match_responses(requests, responses)
    assert list(out.keys()) == ["r1", "r2", "r3"]
    assert out["r1"]["body"] == b"1"
    assert out["r2"]["body"] == b"2"
    assert out["r3"]["body"] == b"3"
