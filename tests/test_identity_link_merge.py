import pytest

import identity_link_merge as ilm


def build(**kwargs):
    merger = ilm.IdentityLinkMerger(**kwargs)
    primary = merger.register(
        "https://idp-a", "sub-a", "alice@example.com", email_verified=True,
        groups=("eng",),
    )
    secondary = merger.register(
        "https://idp-a", "sub-b", "bob@example.com", email_verified=True,
        groups=("ops",),
    )
    return merger, primary, secondary


def test_same_email_across_issuers_is_a_conflict_not_auto_merge():
    merger = ilm.IdentityLinkMerger()
    one = merger.register("https://idp-a", "sub-a", "same@example.com", email_verified=True)
    two = merger.register("https://idp-b", "sub-b", "SAME@example.com", email_verified=True)
    plan = merger.plan_merge(one["key"], two["key"])
    assert plan["status"] == "conflict"
    assert plan["reason"] == "email_collision"
    assert set(plan["candidates"]) == {one["key"], two["key"]}


def test_unverified_email_cannot_be_merge_basis():
    merger, primary, _ = build()
    secondary = merger.register(
        "https://idp-a", "sub-c", "carol@example.com", email_verified=False
    )
    plan = merger.plan_merge(primary["key"], secondary["key"])
    assert plan["status"] == "conflict"
    assert plan["reason"] == "email_unverified"


def test_merge_invalidates_secondary_session():
    merger, primary, secondary = build()
    merger.add_session(secondary["key"], "sess-b")
    result = merger.merge(primary["key"], secondary["key"])
    assert result["status"] == "merged"
    assert merger.session_valid(secondary["key"], "sess-b") is False


def test_old_refresh_token_is_rejected_after_merge():
    merger, primary, secondary = build()
    merger.add_refresh_token(secondary["key"], "rt-b")
    merger.merge(primary["key"], secondary["key"])
    assert merger.exchange_refresh(secondary["key"], "rt-b") is False


def test_unmerge_restores_archived_account_and_groups():
    merger, primary, secondary = build()
    result = merger.merge(primary["key"], secondary["key"])
    assert merger.get(secondary["key"]) is None
    restored = merger.unmerge(result["merge_id"])
    assert restored["status"] == "unmerged"
    account = merger.get(secondary["key"])
    assert account["email"] == "bob@example.com"
    assert set(account["groups"]) == {"ops"}
    assert set(merger.get(primary["key"])["groups"]) == {"eng"}


def test_merge_writes_replayable_audit_row():
    merger, primary, secondary = build()
    result = merger.merge(primary["key"], secondary["key"])
    rows = merger.history()
    assert len(rows) == 1
    row = rows[0]
    assert row["merge_id"] == result["merge_id"]
    assert row["primary"] == primary["key"]
    assert row["secondary"] == secondary["key"]
    assert {"invalidate_sessions", "invalidate_tokens", "move_groups"} <= set(row["actions"])


def test_email_case_is_normalized_and_reuses_account():
    merger = ilm.IdentityLinkMerger()
    account = merger.register(
        "https://idp-a", "sub-a", "Alice@Example.com", email_verified=True
    )
    assert account["email"] == "alice@example.com"
    found = merger.resolve(
        "https://idp-a", "sub-b", "ALICE@EXAMPLE.COM", email_verified=True
    )
    assert found["key"] == account["key"]
    assert len(merger.accounts()) == 1


def test_changed_sub_reuses_verified_email_account():
    merger = ilm.IdentityLinkMerger()
    merger.register("https://idp-a", "sub-old", "carol@example.com", email_verified=True)
    found = merger.resolve(
        "https://idp-a", "sub-new", "carol@example.com", email_verified=True
    )
    assert found["key"] == ("https://idp-a", "sub-old")
    assert merger.get(("https://idp-a", "sub-new")) is None
    assert len(merger.accounts()) == 1


def test_force_merge_requires_second_confirmation():
    merger, primary, secondary = build()
    pending = merger.merge(primary["key"], secondary["key"], force=True)
    assert pending["status"] == "confirmation_required"
    assert merger.get(secondary["key"]) is not None
    done = merger.merge(
        primary["key"], secondary["key"], force=True, confirmation="CONFIRM"
    )
    assert done["status"] == "merged"


def test_group_membership_is_migrated_without_loss():
    merger, primary, secondary = build()
    result = merger.merge(primary["key"], secondary["key"])
    assert result["groups_moved"] == 1
    assert set(merger.get(primary["key"])["groups"]) == {"eng", "ops"}
    assert merger.history()[0]["groups_moved"] == 1


def test_repeated_merge_is_idempotent():
    merger, primary, secondary = build()
    first = merger.merge(primary["key"], secondary["key"])
    second = merger.merge(primary["key"], secondary["key"])
    assert second["status"] == "already_merged"
    assert second["merge_id"] == first["merge_id"]
    assert len(merger.history()) == 1


def test_empty_issuer_is_rejected():
    merger = ilm.IdentityLinkMerger()
    with pytest.raises(ValueError, match="issuer"):
        merger.register("", "sub-a", "alice@example.com", email_verified=True)


def test_cross_tenant_merge_is_rejected():
    merger = ilm.IdentityLinkMerger()
    one = merger.register("https://idp-a", "sub-a", "alice@example.com", email_verified=True, tenant="t1")
    two = merger.register("https://idp-a", "sub-b", "bob@example.com", email_verified=True, tenant="t2")
    plan = merger.plan_merge(one["key"], two["key"])
    assert plan["status"] == "conflict"
    assert plan["reason"] == "tenant_mismatch"


def test_commit_failure_rolls_back_everything():
    merger, primary, secondary = build()
    merger.add_session(secondary["key"], "sess-b")
    merger.inject_fault("commit")
    with pytest.raises(ilm.MergeFailed):
        merger.merge(primary["key"], secondary["key"])
    assert merger.get(secondary["key"]) is not None
    assert set(merger.get(primary["key"])["groups"]) == {"eng"}
    assert merger.history() == []
    assert merger.session_valid(secondary["key"], "sess-b") is True


def test_snapshot_is_isolated_from_later_merge():
    merger, primary, secondary = build()
    snap = merger.snapshot()
    assert snap[secondary["key"]]["email"] == "bob@example.com"
    merger.merge(primary["key"], secondary["key"])
    assert merger.get(secondary["key"]) is None
    assert snap[secondary["key"]]["email"] == "bob@example.com"
