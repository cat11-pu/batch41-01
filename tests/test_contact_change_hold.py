import pytest

import contact_change_hold as cch


def setup_hold(**kwargs):
    hold = cch.ContactChangeHold(**kwargs)
    hold.register_account("alice", "old@example.com")
    hold.add_session("alice", "sess-1")
    hold.add_recovery_code("alice", "RC-1")
    return hold


def start(hold, new_contact="new@example.com", session="sess-1", now=0):
    return hold.request_change("alice", new_contact, session, now=now)


def complete(hold, now=0):
    change = start(hold, now=now)
    hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1", now=now)
    return hold.confirm_change(change["change_id"], change["tokens"][1], "sess-1", now=now)


def test_old_address_is_notified_before_confirmation():
    hold = setup_hold()
    change = start(hold)
    notices = hold.notifications("old@example.com")
    assert any(n["kind"] == "change_requested_old" and n["change_id"] == change["change_id"] for n in notices)
    assert hold.contact("alice") == "old@example.com"


def test_same_token_cannot_confirm_twice():
    hold = setup_hold()
    change = start(hold)
    first = hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1")
    assert first["status"] == "pending_second_confirmation"
    with pytest.raises(ValueError, match="duplicate_confirmation"):
        hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1")
    done = hold.confirm_change(change["change_id"], change["tokens"][1], "sess-1")
    assert done["status"] == "completed"


def test_confirmation_token_expires_quickly():
    hold = setup_hold(token_ttl=900)
    change = start(hold, now=0)
    with pytest.raises(ValueError, match="token_expired"):
        hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1", now=901)


def test_old_sessions_are_invalid_after_completion():
    hold = setup_hold()
    complete(hold)
    assert hold.contact("alice") == "new@example.com"
    assert hold.session_valid("alice", "sess-1") is False


def test_second_concurrent_request_is_rejected():
    hold = setup_hold()
    start(hold, new_contact="first@example.com")
    with pytest.raises(ValueError, match="change_pending"):
        start(hold, new_contact="second@example.com")
    assert hold.notifications("second@example.com") == []


def test_cooldown_blocks_another_change():
    hold = setup_hold()
    complete(hold, now=0)
    with pytest.raises(ValueError, match="cooldown_active"):
        start(hold, new_contact="later@example.com", now=100)


def test_recovery_codes_are_invalid_after_completion():
    hold = setup_hold()
    assert hold.recovery_valid("alice", "RC-1") is True
    complete(hold)
    assert hold.recovery_valid("alice", "RC-1") is False


def test_admin_change_also_obeys_cooldown():
    hold = setup_hold()
    complete(hold, now=0)
    with pytest.raises(ValueError, match="cooldown_active"):
        hold.admin_change("alice", "admin@example.com", "admin-1", now=100)
    hold.admin_change("alice", "admin@example.com", "admin-1", now=86400)
    assert hold.contact("alice") == "admin@example.com"
    rows = [row for row in hold.audit() if row["action"] == "admin_changed"]
    assert rows and rows[-1]["actor"] == "admin-1"


def test_email_is_normalized_to_lower_case():
    hold = setup_hold()
    change = start(hold, new_contact="Bob@Example.COM")
    hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1")
    hold.confirm_change(change["change_id"], change["tokens"][1], "sess-1")
    assert hold.contact("alice") == "bob@example.com"
    with pytest.raises(ValueError, match="contact_unchanged"):
        hold.request_change("alice", "BOB@example.com", "sess-1", now=90000)


def test_confirmation_is_bound_to_the_original_session():
    hold = setup_hold()
    change = start(hold, session="sess-1")
    with pytest.raises(ValueError, match="session_mismatch"):
        hold.confirm_change(change["change_id"], change["tokens"][0], "sess-2")


def test_empty_address_is_rejected():
    hold = setup_hold()
    with pytest.raises(ValueError, match="empty_contact"):
        start(hold, new_contact="")


def test_token_cannot_be_reused_for_another_change():
    hold = setup_hold(cooldown=0)
    first = start(hold, new_contact="new@example.com")
    hold.confirm_change(first["change_id"], first["tokens"][0], "sess-1")
    hold.confirm_change(first["change_id"], first["tokens"][1], "sess-1")
    second = start(hold, new_contact="later@example.com", now=10)
    with pytest.raises(ValueError, match="token_used"):
        hold.confirm_change(second["change_id"], first["tokens"][0], "sess-1", now=10)


def test_commit_failure_rolls_back_contact_and_sessions():
    hold = setup_hold()
    change = start(hold)
    hold.confirm_change(change["change_id"], change["tokens"][0], "sess-1")
    hold.inject_fault("commit")
    with pytest.raises(cch.ChangeFailed):
        hold.confirm_change(change["change_id"], change["tokens"][1], "sess-1")
    assert hold.contact("alice") == "old@example.com"
    assert hold.session_valid("alice", "sess-1") is True
    assert hold.recovery_valid("alice", "RC-1") is True
    assert hold.change_status(change["change_id"]) != "completed"


def test_audit_records_each_step():
    hold = setup_hold()
    complete(hold)
    actions = {row["action"] for row in hold.audit()}
    assert {"requested", "old_notified", "new_notified", "confirmation", "completed"} <= actions


def test_holds_are_isolated_from_each_other():
    first = setup_hold()
    second = cch.ContactChangeHold()
    second.register_account("alice", "other@example.com")
    complete(first)
    assert first.contact("alice") == "new@example.com"
    assert second.contact("alice") == "other@example.com"
    assert first.notifications("old@example.com")
