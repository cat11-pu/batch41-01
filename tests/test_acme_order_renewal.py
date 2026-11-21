import pytest

import acme_order_renewal as aor


DAY = 86400


def ready_order(engine, domains=("a.example.com",), account_key_id=None):
    order = engine.start_order(list(domains), account_key_id=account_key_id)
    for index, domain in enumerate(domains):
        engine.add_challenge(order["id"], domain, "ch-%d" % (index + 1), now=0)
        engine.respond_challenge(order["id"], "ch-%d" % (index + 1), valid=True, now=5)
    return order["id"]


def test_download_before_valid_is_refused():
    engine = aor.AcmeOrderRenewal()
    order = engine.start_order(["a.example.com"])
    with pytest.raises(aor.AcmeError) as caught:
        engine.download(order["id"])
    assert caught.value.reason == "order_not_valid"


def test_nonce_cannot_be_replayed():
    engine = aor.AcmeOrderRenewal()
    value = engine.nonce()
    assert engine.consume_nonce(value)["ok"] is True
    second = engine.consume_nonce(value)
    assert second["ok"] is False
    assert second["reason"] == "bad_nonce"


def test_order_becomes_ready_after_valid_challenge():
    engine = aor.AcmeOrderRenewal()
    order = engine.start_order(["a.example.com"])
    engine.add_challenge(order["id"], "a.example.com", "ch-1", now=0)
    result = engine.respond_challenge(order["id"], "ch-1", valid=True, now=10)
    assert result["status"] == "valid"
    assert engine.get_order(order["id"])["status"] == "ready"


def test_multi_domain_order_needs_every_authorization():
    engine = aor.AcmeOrderRenewal()
    order = engine.start_order(["a.example.com", "b.example.com"])
    engine.add_challenge(order["id"], "a.example.com", "ch-a", now=0)
    engine.add_challenge(order["id"], "b.example.com", "ch-b", now=0)
    engine.respond_challenge(order["id"], "ch-a", valid=True, now=5)
    result = engine.finalize(order["id"])
    assert result["status"] == "pending"
    assert result["error"] == "authorizations_incomplete"


def test_cname_chain_checks_every_hop():
    engine = aor.AcmeOrderRenewal(allowed_zones=("example.com",))
    good = {"a.example.com": "b.example.com"}
    bad = {"a.example.com": "b.example.com", "b.example.com": "evil.other.com"}
    assert engine.authorize_dns("a.example.com", good) is True
    assert engine.authorize_dns("a.example.com", bad) is False


def test_renewal_window_is_based_on_expiry():
    engine = aor.AcmeOrderRenewal(renewal_days=30)
    not_after = 90 * DAY
    assert engine.renewal_due(not_after, 30 * DAY) is False
    assert engine.renewal_due(not_after, 65 * DAY) is True


def test_older_certificate_cannot_overwrite_newer_one():
    engine = aor.AcmeOrderRenewal()
    first = engine.store_certificate("cert-1", ["leaf-new", "intermediate-ca"], issued_at=100, not_after=200)
    assert first["stored"] is True
    stale = engine.store_certificate("cert-1", ["leaf-old", "intermediate-ca"], issued_at=90, not_after=180)
    assert stale["stored"] is False
    assert stale["reason"] == "stale_certificate"
    assert engine.current_certificate("cert-1")["issued_at"] == 100


def test_downloaded_chain_is_leaf_first():
    engine = aor.AcmeOrderRenewal(intermediates=("intermediate-ca",))
    order_id = ready_order(engine)
    engine.finalize(order_id)
    chain = engine.download(order_id)["chain"]
    assert chain == ["leaf-" + order_id, "intermediate-ca"]


def test_finalize_rejects_wrong_account_key():
    engine = aor.AcmeOrderRenewal(account_key_id="key-1")
    order_id = ready_order(engine)
    result = engine.finalize(order_id, account_key_id="old-key")
    assert result["status"] == "invalid"
    assert result["error"] == "account_key_mismatch"
    assert engine.get_order(order_id)["status"] == "ready"
    assert engine.finalize(order_id)["status"] == "valid"


def test_revoked_domain_cannot_start_renewal():
    engine = aor.AcmeOrderRenewal()
    engine.revoke("bad.example.com")
    with pytest.raises(aor.AcmeError) as caught:
        engine.start_order(["bad.example.com"])
    assert caught.value.reason == "domain_revoked"


def test_empty_domain_list_is_rejected():
    engine = aor.AcmeOrderRenewal()
    with pytest.raises(ValueError, match="empty_domains"):
        engine.start_order([])


def test_challenge_timeout_invalidates_order():
    engine = aor.AcmeOrderRenewal(challenge_timeout=300)
    order = engine.start_order(["a.example.com"])
    engine.add_challenge(order["id"], "a.example.com", "ch-1", now=0)
    result = engine.respond_challenge(order["id"], "ch-1", valid=True, now=301)
    assert result["status"] == "invalid"
    assert result["reason"] == "challenge_timeout"
    assert engine.get_order(order["id"])["status"] == "invalid"


def test_invalid_authorization_blocks_finalize():
    engine = aor.AcmeOrderRenewal()
    order = engine.start_order(["a.example.com", "b.example.com"])
    engine.set_authorization(order["id"], "a.example.com", "valid")
    engine.set_authorization(order["id"], "b.example.com", "valid")
    assert engine.get_order(order["id"])["status"] == "ready"
    engine.set_authorization(order["id"], "a.example.com", "invalid")
    assert engine.get_order(order["id"])["status"] == "invalid"
    result = engine.finalize(order["id"])
    assert result["error"] == "invalid_authorization"


def test_duplicate_finalize_is_idempotent():
    engine = aor.AcmeOrderRenewal()
    order_id = ready_order(engine)
    first = engine.finalize(order_id)
    second = engine.finalize(order_id)
    assert second["status"] == "valid"
    assert second["duplicate"] is True
    assert second["chain"] == first["chain"]


def test_engines_and_returned_orders_are_isolated():
    one = aor.AcmeOrderRenewal()
    two = aor.AcmeOrderRenewal()
    order = one.start_order(["a.example.com"])
    order["domains"].append("sneaky.example.com")
    assert one.get_order(order["id"])["domains"] == ["a.example.com"]
    with pytest.raises(KeyError):
        two.get_order(order["id"])
