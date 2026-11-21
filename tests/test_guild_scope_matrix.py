import pytest

import guild_scope_matrix as gsm


def build_matrix():
    matrix = gsm.ScopeMatrix()
    matrix.create_guild("g1", "leader", member_cap=5)
    matrix.create_role(
        "g1", "vice", "Vice", rank=50,
        permissions={"warehouse", "manage_roles"},
        channels={"warehouse": {"guild"}},
    )
    matrix.create_role("g1", "officer", "Officer", rank=40,
                       permissions={"chat"})
    matrix.create_role("g1", "custom", "Custom", rank=10,
                       permissions={"chat"}, parent="vice")
    return matrix


def test_role_inherits_parent_permissions():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "custom", actor="leader")
    assert matrix.check("g1", "alice", "chat") is True
    assert matrix.check("g1", "alice", "warehouse", channel="guild") is True


def test_deleting_parent_role_does_not_blank_members():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "custom", actor="leader")
    matrix.delete_role("g1", "vice", actor="leader")
    assert matrix.check("g1", "alice", "warehouse", channel="guild") is True


def test_permission_cache_is_invalidated_by_revoke():
    matrix = build_matrix()
    matrix.join("g1", "bob")
    matrix.assign_role("g1", "bob", "officer", actor="leader")
    assert matrix.check("g1", "bob", "chat") is True
    matrix.revoke("g1", "officer", "chat", actor="leader")
    assert matrix.check("g1", "bob", "chat") is False


def test_kicked_member_is_denied_immediately():
    matrix = build_matrix()
    matrix.join("g1", "bob")
    matrix.assign_role("g1", "bob", "officer", actor="leader")
    matrix.grant("g1", "officer", "warehouse", actor="leader")
    assert matrix.check("g1", "bob", "warehouse") is True
    matrix.kick("g1", "bob", actor="leader")
    assert matrix.check("g1", "bob", "warehouse") is False


def test_same_account_has_same_permissions_in_every_zone():
    matrix = build_matrix()
    matrix.join("g1", "acct-1")
    matrix.assign_role("g1", "acct-1", "vice", actor="leader")
    assert matrix.check("g1", "acct-1", "warehouse",
                        channel="guild", zone="z1") is True
    assert matrix.check("g1", "acct-1", "warehouse",
                        channel="guild", zone="z2") is True


def test_member_cap_is_checked_atomically():
    matrix = gsm.ScopeMatrix()
    matrix.create_guild("g1", "leader", member_cap=2)
    matrix.join("g1", "b")
    with pytest.raises(ValueError):
        matrix.join("g1", "c")


def test_kick_releases_borrowed_resource_locks():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "custom", actor="leader")
    assert matrix.borrow_lock("g1", "alice", "stash", now=0.0) is not None
    matrix.kick("g1", "alice", actor="leader")
    matrix.join("g1", "newbie")
    assert matrix.borrow_lock("g1", "newbie", "stash", now=1.0) is not None


def test_role_cannot_grant_permission_to_itself():
    matrix = build_matrix()
    matrix.join("g1", "bob")
    matrix.assign_role("g1", "bob", "vice", actor="leader")
    with pytest.raises(ValueError):
        matrix.grant("g1", "vice", "audit", actor="bob")


def test_role_cannot_grant_to_an_equal_rank_role():
    matrix = build_matrix()
    matrix.create_role("g1", "vice2", "Vice2", rank=50, permissions={"chat"})
    matrix.join("g1", "bob")
    matrix.assign_role("g1", "bob", "vice", actor="leader")
    matrix.join("g1", "dave")
    matrix.assign_role("g1", "dave", "vice2", actor="leader")
    with pytest.raises(ValueError):
        matrix.grant("g1", "vice2", "audit", actor="bob")
    matrix.grant("g1", "vice2", "audit", actor="leader")
    assert matrix.check("g1", "dave", "audit") is True


def test_channel_scope_is_part_of_permission_check():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "custom", actor="leader")
    assert matrix.check("g1", "alice", "warehouse", channel="guild") is True
    assert matrix.check("g1", "alice", "warehouse",
                        channel="announcement") is False


def test_dissolve_invalidates_pending_approvals_and_blocks_join():
    matrix = build_matrix()
    request = matrix.request_join("g1", "pending")
    matrix.dissolve("g1", actor="leader")
    with pytest.raises(ValueError):
        matrix.approve("g1", request["request_id"], actor="leader")
    with pytest.raises(ValueError):
        matrix.join("g1", "late")


def test_audit_keeps_role_id_after_rename():
    matrix = build_matrix()
    matrix.grant("g1", "officer", "audit", actor="leader")
    matrix.rename_role("g1", "officer", "Auditor", actor="leader")
    entries = matrix.audit("g1")
    assert any(entry["role_id"] == "officer" for entry in entries)


def test_stale_version_action_is_rejected_after_permission_change():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "officer", actor="leader")
    matrix.grant("g1", "officer", "warehouse", actor="leader")
    version = matrix.version("g1")
    result = matrix.act("g1", "alice", "warehouse", "open_stash",
                        expected_version=version)
    assert result["allowed"] is True
    matrix.revoke("g1", "officer", "chat", actor="leader")
    with pytest.raises(ValueError):
        matrix.act("g1", "alice", "warehouse", "open_stash",
                   expected_version=version)


def test_leader_transfer_has_a_cooldown():
    matrix = build_matrix()
    matrix.join("g1", "heir")
    result = matrix.transfer_leader("g1", "leader", "heir", now=0.0)
    assert result["leader"] == "heir"
    with pytest.raises(ValueError):
        matrix.transfer_leader("g1", "heir", "leader", now=100.0)
    matrix.transfer_leader("g1", "heir", "leader", now=3600.0)
    assert matrix.leader("g1") == "leader"


def test_announcement_edit_is_denied_after_revoke():
    matrix = build_matrix()
    matrix.join("g1", "alice")
    matrix.assign_role("g1", "alice", "officer", actor="leader")
    matrix.grant("g1", "officer", "edit_announcement", actor="leader",
                 channel="announcement")
    version = matrix.version("g1")
    assert matrix.check("g1", "alice", "edit_announcement",
                        channel="announcement") is True
    assert matrix.act("g1", "alice", "edit_announcement", "edit",
                      expected_version=version,
                      channel="announcement")["allowed"] is True
    matrix.revoke("g1", "officer", "edit_announcement", actor="leader")
    assert matrix.check("g1", "alice", "edit_announcement",
                        channel="announcement") is False
    with pytest.raises(PermissionError):
        matrix.act("g1", "alice", "edit_announcement", "edit",
                   expected_version=matrix.version("g1"),
                   channel="announcement")
