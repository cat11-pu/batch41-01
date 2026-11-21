import re

import pytest

import session_ticket_epoch as tse


def keeper(path=None, **over):
    args = {"grace": 60, "ticket_ttl": 600, "early_data_limit": 8}
    args.update(over)
    return tse.TicketEpochKeeper(state_path=path, **args)


def ready(path=None):
    store = keeper(path)
    store.bind_identity("fp1", "alice")
    return store


def issue(store, now=0):
    return store.issue("fp1", "h2", now)


def test_issue_returns_ticket_bound_to_epoch_and_key():
    store = ready()
    issued = issue(store)
    assert issued["ticket"]
    assert issued["epoch"] == 1
    assert re.fullmatch(r"[0-9a-f]{16}", issued["key_id"])
    assert issued["expires_at"] == 600


def test_resume_valid_ticket_returns_identity_and_epoch():
    store = ready()
    issued = issue(store)
    result = store.resume(issued["ticket"], "fp1", "h2", now=1)
    assert result["ok"] is True
    assert result["identity"] == "alice"
    assert result["epoch"] == 1
    assert result["reject_reason"] is None


def test_resume_rejects_mismatched_client_certificate():
    store = ready()
    issued = issue(store)
    result = store.resume(issued["ticket"], "fp2", "h2", now=1)
    assert result["ok"] is False
    assert result["reject_reason"] == "cert_mismatch"


def test_resume_rejects_revoked_certificate_and_bad_chain():
    store = ready()
    issued = issue(store)
    store.revoke_cert("fp1")
    revoked = store.resume(issued["ticket"], "fp1", "h2", now=1)
    assert revoked["reject_reason"] == "revoked"

    other = ready()
    issued_other = issue(other)
    bad_chain = other.resume(issued_other["ticket"], "fp1", "h2", now=1, chain_ok=False)
    assert bad_chain["reject_reason"] == "bad_chain"


def test_resume_rejects_alpn_mismatch():
    store = ready()
    issued = issue(store)
    result = store.resume(issued["ticket"], "fp1", "h3", now=1)
    assert result["ok"] is False
    assert result["reject_reason"] == "alpn_mismatch"


def test_old_epoch_ticket_works_during_grace():
    store = ready()
    issued = issue(store)
    store.rotate(10)
    result = store.resume(issued["ticket"], "fp1", "h2", now=30)
    assert result["ok"] is True
    assert result["epoch"] == 1


def test_old_epoch_key_is_removed_after_grace():
    store = ready()
    issued = issue(store)
    store.rotate(10)
    result = store.resume(issued["ticket"], "fp1", "h2", now=71)
    assert result["ok"] is False
    assert result["reject_reason"] == "unknown_epoch"


def test_expired_ticket_is_rejected():
    store = ready()
    issued = issue(store)
    result = store.resume(issued["ticket"], "fp1", "h2", now=601)
    assert result["ok"] is False
    assert result["reject_reason"] == "expired"


def test_zero_rtt_replay_is_rejected():
    store = ready()
    issued = issue(store)
    first = store.resume(
        issued["ticket"], "fp1", "h2", method="GET", early_data=b"abc",
        replay_key="nonce-1", now=1,
    )
    second = store.resume(
        issued["ticket"], "fp1", "h2", method="GET", early_data=b"abc",
        replay_key="nonce-1", now=2,
    )
    assert first["ok"] is True
    assert first["early_data_accepted"] is True
    assert second["ok"] is False
    assert second["reject_reason"] == "replay"


def test_zero_rtt_rejects_unsafe_method():
    store = ready()
    issued = issue(store)
    result = store.resume(
        issued["ticket"], "fp1", "h2", method="POST", early_data=b"abc",
        replay_key="nonce-2", now=1,
    )
    assert result["ok"] is False
    assert result["reject_reason"] == "unsafe_method"


def test_early_data_over_limit_is_rejected():
    store = ready()
    issued = issue(store)
    result = store.resume(
        issued["ticket"], "fp1", "h2", method="GET", early_data=b"0123456789",
        replay_key="nonce-3", now=1,
    )
    assert result["ok"] is False
    assert result["reject_reason"] == "early_data_too_large"


def test_empty_or_malformed_ticket_is_rejected():
    store = ready()
    empty = store.resume("", "fp1", "h2", now=1)
    malformed = store.resume("not-a-ticket!!", "fp1", "h2", now=1)
    assert empty["reject_reason"] == "empty_ticket"
    assert malformed["reject_reason"] == "malformed_ticket"


def test_key_ids_are_unique_across_rotations():
    store = ready()
    first = issue(store)
    second_epoch = store.rotate(0)
    third_epoch = store.rotate(0)
    ids = {first["key_id"], second_epoch["key_id"], third_epoch["key_id"]}
    assert len(ids) == 3
    fresh = store.issue("fp1", "h2", 1)
    resumed = store.resume(fresh["ticket"], "fp1", "h2", now=2)
    assert resumed["ok"] is True
    assert resumed["key_id"] == third_epoch["key_id"]


def test_state_survives_flush_and_reload(tmp_path):
    path = str(tmp_path / "tickets.json")
    store = ready(path)
    issued = issue(store)
    store.rotate(10)
    store.flush()
    reborn = tse.TicketEpochKeeper(
        state_path=path, grace=60, ticket_ttl=600, early_data_limit=8
    )
    result = reborn.resume(issued["ticket"], "fp1", "h2", now=20)
    assert result["ok"] is True
    assert result["identity"] == "alice"
    assert reborn.snapshot()["epoch"] == 2


def test_identity_rebinding_and_ledger_isolation():
    store = ready()
    issued = issue(store)
    store.bind_identity("fp1", "bob")
    resumed = store.resume(issued["ticket"], "fp1", "h2", now=1)
    assert resumed["identity"] == "bob"
    rejected = store.resume(issued["ticket"], "fp2", "h2", now=2)
    assert rejected["reject_reason"] == "cert_mismatch"
    events_before = store.snapshot()["ledger_events"]
    copy = store.ledger()
    assert any(event["reason"] == "cert_mismatch" for event in copy)
    copy.clear()
    assert store.snapshot()["ledger_events"] == events_before
