import pytest

import support_impersonation_grant as sig


def book(**kwargs):
    return sig.ImpersonationGrantBook(**kwargs)


def pending(book, ticket="T-1", agent="agent-1", target="alice", ttl=600, now=0):
    book.open_ticket(ticket)
    return book.request_grant(ticket, agent, target, ttl=ttl, now=now)


def active(book, ticket="T-1", agent="agent-1", target="alice", ttl=600, now=0):
    grant = pending(book, ticket=ticket, agent=agent, target=target, ttl=ttl, now=now)
    book.approve(grant["grant_id"], "manager-1", now=now)
    return book.approve(grant["grant_id"], "manager-2", now=now)


def test_closing_ticket_revokes_live_grant():
    grants = book()
    grant = active(grants)
    assert grants.check_access(grant["grant_id"]) is True
    grants.close_ticket("T-1")
    assert grants.check_access(grant["grant_id"]) is False
    assert grants.get_grant(grant["grant_id"])["status"] == "revoked"


def test_write_scope_is_rejected():
    grants = book()
    grants.open_ticket("T-1")
    with pytest.raises(sig.GrantError) as caught:
        grants.request_grant("T-1", "agent-1", "alice", scopes=("write",))
    assert caught.value.reason == "write_not_allowed"


def test_concurrent_grant_limit_is_enforced():
    grants = book(max_concurrent=3)
    for index in range(3):
        grants.open_ticket("T-%d" % index)
        grants.request_grant("T-%d" % index, "agent-1", "alice")
    grants.open_ticket("T-extra")
    with pytest.raises(sig.GrantError) as caught:
        grants.request_grant("T-extra", "agent-1", "alice")
    assert caught.value.reason == "concurrency_limit"


def test_one_approver_is_not_enough():
    grants = book()
    grant = pending(grants)
    grants.approve(grant["grant_id"], "manager-1")
    assert grants.check_access(grant["grant_id"]) is False
    assert grants.get_grant(grant["grant_id"])["status"] == "pending_approval"


def test_target_user_receives_notification():
    grants = book()
    grant = active(grants)
    notices = grants.notifications("alice")
    assert any(n["kind"] == "impersonation_started" and n["grant_id"] == grant["grant_id"] for n in notices)
    assert any(n["kind"] == "impersonation_started" for n in grants.notifications("agent-1"))


def test_revoke_propagates_to_issued_token():
    grants = book()
    grant = active(grants)
    token = grants.issue_token(grant["grant_id"])
    assert grants.check_token(token) is True
    grants.revoke(grant["grant_id"])
    assert grants.check_token(token) is False
    assert grants.check_access(grant["grant_id"]) is False


def test_impersonation_token_uses_separate_audience():
    grants = book()
    grant = active(grants)
    token = grants.issue_token(grant["grant_id"])
    assert token["aud"] == "impersonation"
    assert token["aud"] != "user"
    forged = {"aud": "user", "grant_id": grant["grant_id"], "sub": "alice", "expires_at": 999}
    assert grants.check_token(forged) is False


def test_ticket_reassignment_revokes_grant():
    grants = book()
    grant = active(grants)
    grants.reassign_ticket("T-1", "other-agent")
    assert grants.check_access(grant["grant_id"]) is False
    assert grants.get_grant(grant["grant_id"])["status"] == "revoked"


def test_negative_ttl_is_rejected():
    grants = book()
    grants.open_ticket("T-1")
    with pytest.raises(sig.GrantError) as caught:
        grants.request_grant("T-1", "agent-1", "alice", ttl=-1)
    assert caught.value.reason == "invalid_ttl"


def test_audit_rows_name_the_target_user():
    grants = book()
    grant = active(grants)
    grants.check_access(grant["grant_id"])
    rows = [row for row in grants.audit() if row["action"] == "activated"]
    assert rows
    assert rows[0]["target"] == "alice"
    assert rows[0]["agent"] == "agent-1"


def test_expired_grant_denies_access():
    grants = book()
    grant = active(grants, ttl=10, now=0)
    assert grants.check_access(grant["grant_id"], now=11) is False
    assert grants.get_grant(grant["grant_id"])["status"] == "expired"


def test_same_approver_cannot_count_twice():
    grants = book()
    grant = pending(grants)
    grants.approve(grant["grant_id"], "manager-1")
    with pytest.raises(sig.GrantError) as caught:
        grants.approve(grant["grant_id"], "manager-1")
    assert caught.value.reason == "duplicate_approver"
    assert grants.get_grant(grant["grant_id"])["status"] == "pending_approval"
    assert grants.approve(grant["grant_id"], "manager-2")["status"] == "active"


def test_empty_ticket_id_is_rejected():
    grants = book()
    with pytest.raises(sig.GrantError) as caught:
        grants.request_grant("", "agent-1", "alice")
    assert caught.value.reason == "empty_ticket"


def test_cross_tenant_ticket_is_rejected():
    grants = book(tenant="t1")
    grants.open_ticket("T-2", tenant="t2")
    with pytest.raises(sig.GrantError) as caught:
        grants.request_grant("T-2", "agent-1", "alice")
    assert caught.value.reason == "tenant_mismatch"


def test_books_and_returned_grants_are_isolated():
    first = book()
    second = book()
    grant = active(first)
    with pytest.raises(KeyError):
        second.get_grant(grant["grant_id"])
    copy = first.get_grant(grant["grant_id"])
    copy["scopes"].append("write")
    assert first.get_grant(grant["grant_id"])["scopes"] == ["read"]
