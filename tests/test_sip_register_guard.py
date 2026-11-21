import pytest

import sip_register_guard as sg

AOR = "alice@example.com"


def contact(uri="sip:alice@10.0.0.5:5060", expires=600, q=1.0):
    return {"uri": uri, "expires": expires, "q": q}


def auth(nonce, uri="sip:alice@example.com", qop="auth"):
    return {"username": "alice", "uri": uri, "nonce": nonce, "qop": qop}


def request(call_id="c1", cseq=1, branch="z9hG4bK-a", transport="UDP",
            contacts=None, expires=600, authorization=None,
            source="10.0.0.5:5060", aor=AOR, request_uri="sip:alice@example.com"):
    return {
        "method": "REGISTER",
        "aor": aor,
        "request_uri": request_uri,
        "call_id": call_id,
        "cseq": cseq,
        "branch": branch,
        "transport": transport,
        "source": source,
        "contacts": [contact()] if contacts is None else contacts,
        "expires": expires,
        "authorization": authorization,
    }


def guard(**kwargs):
    kwargs.setdefault("domains", {"example.com"})
    return sg.RegisterGuard(**kwargs)


def response(call_id, cseq, branch, transport="UDP", status=200):
    return {"call_id": call_id, "cseq": cseq, "branch": branch,
            "transport": transport, "status": status}


def test_first_register_creates_binding():
    g = guard()
    nonce = g.issue_nonce(AOR)
    out = g.register(request(authorization=auth(nonce)), now=0.0)
    assert out["aor"] == AOR
    assert out["contacts"] == ["sip:alice@10.0.0.5:5060"]
    assert out["expires"] == 600
    stored = g.bindings(AOR, now=0.0)
    assert len(stored) == 1
    assert stored[0]["uri"] == "sip:alice@10.0.0.5:5060"


def test_replayed_call_id_and_cseq_is_rejected():
    g = guard()
    g.register(request(call_id="c7", cseq=3, branch="z9hG4bK-1",
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    replay = request(call_id="c7", cseq=3, branch="z9hG4bK-2",
                     authorization=auth(g.issue_nonce(AOR)))
    with pytest.raises(sg.SipError) as exc:
        g.register(replay, now=1.0)
    assert exc.value.code == "replay"


def test_nonce_cannot_be_reused():
    g = guard()
    nonce = g.issue_nonce(AOR)
    g.register(request(call_id="c1", cseq=1, authorization=auth(nonce)), now=0.0)
    again = request(call_id="c1", cseq=2, authorization=auth(nonce))
    with pytest.raises(sg.SipError) as exc:
        g.register(again, now=1.0)
    assert exc.value.code == "stale_nonce"


def test_expires_zero_removes_binding():
    g = guard()
    g.register(request(call_id="c1", cseq=1,
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    out = g.register(request(call_id="c2", cseq=2, contacts=[], expires=0,
                             authorization=auth(g.issue_nonce(AOR))), now=5.0)
    assert out["contacts"] == []
    assert g.bindings(AOR, now=5.0) == []
    assert g.primary_target(AOR, now=5.0) is None


def test_all_contacts_are_stored():
    g = guard()
    contacts = [
        contact("sip:alice@10.0.0.6:5060", 600, 0.5),
        contact("sip:alice@10.0.0.7:5060", 600, 0.9),
    ]
    out = g.register(request(call_id="c1", cseq=1, contacts=contacts,
                             authorization=auth(g.issue_nonce(AOR))), now=0.0)
    assert out["contacts"] == ["sip:alice@10.0.0.6:5060", "sip:alice@10.0.0.7:5060"]
    assert len(g.bindings(AOR, now=0.0)) == 2
    assert set(g.targets(AOR, now=0.0)) == set(out["contacts"])


def test_primary_target_is_newest_terminal():
    g = guard()
    g.register(request(call_id="c1", cseq=1,
                       contacts=[contact("sip:alice@10.0.0.5:5060")],
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    g.register(request(call_id="c2", cseq=2,
                       contacts=[contact("sip:alice@10.0.0.6:5060")],
                       authorization=auth(g.issue_nonce(AOR))), now=10.0)
    assert g.primary_target(AOR, now=10.0) == "sip:alice@10.0.0.6:5060"
    assert g.targets(AOR, now=10.0)[0] == "sip:alice@10.0.0.6:5060"


def test_expired_contact_is_not_targeted():
    g = guard()
    g.register(request(call_id="c1", cseq=1,
                       contacts=[contact("sip:alice@10.0.0.5:5060", 120)],
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    g.register(request(call_id="c2", cseq=2,
                       contacts=[contact("sip:alice@10.0.0.6:5060", 600)],
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    assert g.targets(AOR, now=200.0) == ["sip:alice@10.0.0.6:5060"]
    assert g.primary_target(AOR, now=200.0) == "sip:alice@10.0.0.6:5060"


def test_unknown_branch_does_not_match_transaction():
    g = guard()
    g.register(request(call_id="c9", cseq=7, branch="z9hG4bK-good",
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    assert g.match_response(response("c9", 7, "z9hG4bK-bad")) is None
    matched = g.match_response(response("c9", 7, "z9hG4bK-good"))
    assert matched is not None
    assert matched["call_id"] == "c9"
    assert matched["cseq"] == 7


def test_transaction_survives_transport_switch():
    g = guard()
    g.register(request(call_id="c10", cseq=2, branch="z9hG4bK-t",
                       transport="UDP", authorization=auth(g.issue_nonce(AOR))), now=0.0)
    matched = g.match_response(response("c10", 2, "z9hG4bK-t", transport="TCP"))
    assert matched is not None
    assert matched["transport"] == "TCP"


def test_untrusted_contact_domain_is_rejected():
    g = guard()
    bad = request(contacts=[contact("sip:alice@evil.example.net:5060")],
                  authorization=auth(g.issue_nonce(AOR)))
    with pytest.raises(sg.SipError) as exc:
        g.register(bad, now=0.0)
    assert exc.value.code == "untrusted_domain"


def test_authorization_uri_must_match_request_uri():
    g = guard()
    bad = request(authorization=auth(g.issue_nonce(AOR), uri="sip:alice@evil.example.net"))
    with pytest.raises(sg.SipError) as exc:
        g.register(bad, now=0.0)
    assert exc.value.code == "uri_mismatch"


def test_refresh_below_minimum_is_rejected():
    g = guard(min_refresh=120)
    brief = request(expires=60, authorization=auth(g.issue_nonce(AOR)))
    with pytest.raises(sg.SipError) as exc:
        g.register(brief, now=0.0)
    assert exc.value.code == "interval_too_brief"


def test_qop_other_than_auth_is_rejected():
    g = guard()
    challenge = g.challenge(AOR)
    assert challenge["status"] == 401
    assert challenge["qop"] == "auth"
    for qop in ("auth-int", None):
        bad = request(call_id="q-" + str(qop), cseq=1,
                      authorization=auth(g.issue_nonce(AOR), qop=qop))
        with pytest.raises(sg.SipError) as exc:
            g.register(bad, now=0.0)
        assert exc.value.code == "unsupported_qop"


def test_bulk_registration_is_rate_limited():
    g = guard(bulk_limit=3)
    batch = []
    for i in range(3):
        aor = "alice%d@example.com" % i
        batch.append(request(
            call_id="b%d" % i,
            aor=aor,
            request_uri="sip:" + aor,
            contacts=[contact("sip:alice%d@10.0.0.%d:5060" % (i, i + 1))],
            authorization=auth(g.issue_nonce(aor), uri="sip:" + aor),
        ))
    assert len(g.register_bulk(batch, now=0.0)) == 3
    extra_aor = "alice9@example.com"
    extra = request(
        call_id="b9",
        aor=extra_aor,
        request_uri="sip:" + extra_aor,
        contacts=[contact("sip:alice9@10.0.0.9:5060")],
        authorization=auth(g.issue_nonce(extra_aor), uri="sip:" + extra_aor),
    )
    with pytest.raises(sg.SipError) as exc:
        g.register(extra, now=0.0)
    assert exc.value.code == "rate_limited"


def test_snapshot_is_isolated_from_internal_state():
    g = guard()
    g.register(request(call_id="c1", cseq=1,
                       authorization=auth(g.issue_nonce(AOR))), now=0.0)
    snap = g.snapshot()
    assert snap["bindings"][AOR][0]["uri"] == "sip:alice@10.0.0.5:5060"
    snap["bindings"][AOR][0]["uri"] = "sip:mallory@10.9.9.9:5060"
    assert g.bindings(AOR, now=0.0)[0]["uri"] == "sip:alice@10.0.0.5:5060"
