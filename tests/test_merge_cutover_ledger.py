import pytest

import merge_cutover_ledger as mcl


SOURCE = {
    "c1": {"gold": 100, "bag": {"sword": 1}, "mail": ["m1"],
           "guild_warehouse": {"ore": 2}},
    "c2": {"gold": 200, "bag": {"shield": 1}, "mail": [],
           "guild_warehouse": {}},
    "c3": {"gold": 300, "bag": {}, "mail": ["m3"],
           "guild_warehouse": {"gem": 1}},
}


def ledger():
    return mcl.MergeCutover(SOURCE)


def frozen(instance, characters=None):
    return instance.freeze("mig-1", characters or list(SOURCE))


def test_freeze_blocks_source_writes():
    instance = ledger()
    frozen(instance, ["c1"])
    with pytest.raises(ValueError):
        instance.write_source("c1", "add_gold", 5)


def test_freeze_blocks_auction_and_mail_for_all_characters():
    instance = ledger()
    frozen(instance, ["c1"])
    with pytest.raises(ValueError):
        instance.write_source("c2", "auction", 0)
    with pytest.raises(ValueError):
        instance.write_source("c2", "mail", 0)


def test_duplicate_migration_id_keeps_existing_state():
    instance = ledger()
    first = instance.freeze("mig-1", ["c1"])
    second = instance.freeze("mig-1", ["c2"])
    assert first["frozen"] == ["c1"]
    assert second["idempotent"] is True
    assert second["frozen"] == ["c1"]


def test_copy_resumes_at_intra_batch_offset():
    instance = ledger()
    frozen(instance)
    first = instance.copy("mig-1", "b1", ["c1", "c2", "c3"], limit=2)
    assert first["copied"] == 2
    assert first["offset"] == 2
    second = instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    assert second["copied"] == 1
    assert second["offset"] == 3
    assert instance.target_state("c3")["gold"] == 300


def test_replay_applies_events_in_sequence_order():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    result = instance.replay("mig-1", [
        {"seq": 2, "character_id": "c1", "op": "add_gold", "amount": 20},
        {"seq": 1, "character_id": "c1", "op": "add_gold", "amount": 10},
    ])
    assert result["order"] == [1, 2]
    assert instance.target_state("c1")["gold"] == 130


def test_replay_skips_duplicate_sequences():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    events = [
        {"seq": 1, "character_id": "c1", "op": "add_gold", "amount": 10},
        {"seq": 2, "character_id": "c1", "op": "add_gold", "amount": 20},
    ]
    instance.replay("mig-1", events)
    again = instance.replay("mig-1", events)
    assert again["applied"] == 0
    assert again["skipped"] == 2
    assert instance.target_state("c1")["gold"] == 130


def test_replay_skips_events_behind_watermark():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    instance.replay("mig-1", [
        {"seq": 5, "character_id": "c1", "op": "add_gold", "amount": 10},
    ])
    late = instance.replay("mig-1", [
        {"seq": 3, "character_id": "c1", "op": "add_gold", "amount": 100},
    ])
    assert late["applied"] == 0
    assert late["skipped"] == 1
    assert instance.target_state("c1")["gold"] == 110


def test_verify_checksum_covers_all_player_scopes():
    instance = ledger()
    frozen(instance)
    instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    report = instance.verify("mig-1")
    assert set(report["scopes"]) == {
        "role", "gold", "bag", "mail", "guild_warehouse",
    }


def test_verify_fails_before_copy_and_passes_after():
    instance = ledger()
    frozen(instance)
    assert instance.verify("mig-1")["ok"] is False
    instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    assert instance.verify("mig-1")["ok"] is True


def test_rollback_compensates_and_keeps_post_cutover_gains():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    instance.commit("mig-1")
    instance.write_target("c1", "add_gold", 50)
    assert instance.target_state("c1")["gold"] == 150
    result = instance.rollback("mig-1")
    assert result["status"] == "rolled_back"
    assert result["compensated"] >= 1
    assert result["preserved_ops"] == 1
    assert instance.target_state("c1")["gold"] == 50


def test_commit_reissues_tokens_for_copied_characters():
    instance = ledger()
    frozen(instance)
    instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    result = instance.commit("mig-1")
    assert result["tokens_reissued"] == ["c1", "c2", "c3"]


def test_commit_writes_cutover_outbox_entry():
    instance = ledger()
    frozen(instance)
    instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    result = instance.commit("mig-1")
    assert len(result["outbox"]) == 1
    assert result["outbox"][0]["type"] == "cutover"
    assert result["outbox"][0]["migration_id"] == "mig-1"


def test_commit_releases_source_read_only():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    instance.commit("mig-1")
    state = instance.write_source("c1", "add_gold", 5)
    assert state["gold"] == 105


def test_commit_is_idempotent():
    instance = ledger()
    frozen(instance)
    instance.copy("mig-1", "b1", ["c1", "c2", "c3"])
    first = instance.commit("mig-1")
    second = instance.commit("mig-1")
    assert second["idempotent"] is True
    assert second["tokens_reissued"] == first["tokens_reissued"]
    assert len(second["outbox"]) == 1


def test_rollback_unfreezes_source():
    instance = ledger()
    frozen(instance, ["c1"])
    instance.copy("mig-1", "b1", ["c1"])
    result = instance.rollback("mig-1")
    assert result["status"] == "rolled_back"
    state = instance.write_source("c1", "add_gold", 5)
    assert state["gold"] == 105
