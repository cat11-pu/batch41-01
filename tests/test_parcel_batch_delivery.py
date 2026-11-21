import pytest

import parcel_batch_delivery as pbd


def sample_entries():
    return [
        {"player": "alice", "server": "s1",
         "items": {"gold": 100, "sword": 1}, "bound": True},
        {"player": "bob", "server": "s2",
         "items": {"gold": 50}, "bound": False},
        {"player": "carol", "server": "s1",
         "items": {"gold": 10}, "bound": True},
    ]


def single_entry(player="alice", server="s1", bound=True):
    return {"player": player, "server": server,
            "items": {"gold": 100, "sword": 1}, "bound": bound}


def test_plan_reports_batch_size_without_delivering():
    delivery = pbd.ParcelBatchDelivery()
    result = delivery.plan("B1", sample_entries(), now=0.0)
    assert result["batch_id"] == "B1"
    assert result["planned"] == 3
    assert delivery.inbox("alice") == []


def test_replanning_same_batch_is_idempotent():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    delivery.plan("B1", sample_entries(), now=1.0)
    assert delivery.reconcile("B1")["planned"] == 3
    with pytest.raises(ValueError):
        delivery.plan("B1", sample_entries()[:2], now=2.0)


def test_dispatch_delivers_every_planned_entry():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    result = delivery.dispatch("B1", now=10.0)
    assert result["delivered"] == 3
    assert result["skipped"] == 0
    assert result["done"] is True
    assert len(delivery.inbox("alice")) == 1


def test_expiry_starts_at_actual_dispatch_time():
    delivery = pbd.ParcelBatchDelivery(expire_seconds=100)
    delivery.plan("B1", [single_entry()], now=0.0)
    delivery.dispatch("B1", now=1000.0)
    mail = delivery.inbox("alice")[0]
    assert mail["expires_at"] == 1100.0


def test_full_mailbox_is_skipped_with_a_receipt():
    delivery = pbd.ParcelBatchDelivery(mail_capacity=1)
    delivery.plan("A", [single_entry()], now=0.0)
    delivery.dispatch("A", now=1.0)
    delivery.plan("B", [single_entry()], now=2.0)
    result = delivery.dispatch("B", now=3.0)
    assert result["skipped"] == 1
    assert result["delivered"] == 0
    assert result["done"] is True
    assert len(delivery.inbox("alice")) == 1


def test_reconcile_lists_players_still_missing():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    delivery.dispatch("B1", now=1.0, limit=1)
    report = delivery.reconcile("B1")
    assert report["planned"] == 3
    assert report["delivered"] == 1
    assert report["missing"] == ["bob", "carol"]


def test_binding_attribute_travels_with_attachments():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", [single_entry(bound=True)], now=0.0)
    delivery.dispatch("B1", now=1.0)
    items = delivery.inbox("alice")[0]["items"]
    assert items
    assert all(item["bound"] is True for item in items)


def test_rerunning_dispatch_does_not_duplicate_mail():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    delivery.dispatch("B1", now=1.0)
    second = delivery.dispatch("B1", now=2.0)
    assert second["delivered"] == 0
    assert second["skipped"] == 3
    assert len(delivery.inbox("alice")) == 1


def test_deleted_mail_is_not_resent_on_retry():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    delivery.dispatch("B1", now=1.0)
    mail = delivery.inbox("alice")[0]
    assert delivery.delete_mail(mail["mail_id"], now=2.0) is True
    assert delivery.inbox("alice") == []
    delivery.retry("B1", now=3.0)
    assert delivery.inbox("alice") == []
    assert delivery.reconcile("B1")["delivered"] == 3


def test_cross_server_delivery_uses_global_player_id():
    delivery = pbd.ParcelBatchDelivery(home_server="s1")
    delivery.plan("B1", [single_entry(player="g:alice", server="s2")], now=0.0)
    delivery.dispatch("B1", now=1.0)
    mail = delivery.inbox("g:alice")[0]
    assert mail["player"] == "g:alice"
    assert mail["server"] == "s2"


def test_dispatch_offset_resumes_after_a_crash():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    first = delivery.dispatch("B1", now=1.0, limit=2)
    assert first["processed"] == 2
    assert first["offset"] == 2
    assert first["done"] is False
    second = delivery.dispatch("B1", now=2.0)
    assert second["processed"] == 1
    assert second["done"] is True
    assert len(delivery.inbox("alice")) == 1
    assert len(delivery.inbox("bob")) == 1
    assert len(delivery.inbox("carol")) == 1


def test_partial_claim_keeps_remaining_attachments():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", [single_entry()], now=0.0)
    delivery.dispatch("B1", now=1.0)
    mail = delivery.inbox("alice")[0]
    first = delivery.claim(mail["mail_id"], items=["gold"], now=2.0)
    assert first["claimed"] == {"gold": 100}
    assert first["remaining"] == {"sword": 1}
    assert delivery.inbox("alice")[0]["status"] == "partial"
    second = delivery.claim(mail["mail_id"], now=3.0)
    assert second["claimed"] == {"sword": 1}
    assert delivery.inbox("alice")[0]["status"] == "claimed"


def test_claim_with_unknown_item_changes_nothing():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", [single_entry()], now=0.0)
    delivery.dispatch("B1", now=1.0)
    mail = delivery.inbox("alice")[0]
    with pytest.raises(ValueError):
        delivery.claim(mail["mail_id"], items=["gold", "unknown"], now=2.0)
    assert delivery.inbox("alice")[0]["status"] == "unclaimed"
    assert delivery.inbox("alice")[0]["items"]


def test_revoke_soft_deletes_unclaimed_mail():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", sample_entries(), now=0.0)
    delivery.dispatch("B1", now=1.0)
    result = delivery.revoke("B1", actor="gm", now=2.0)
    assert result["revoked_unclaimed"] == 3
    assert delivery.inbox("alice") == []
    deleted = delivery.inbox("alice", include_deleted=True)[0]
    assert deleted["status"] == "revoked"
    assert deleted["deleted"] is True
    assert any(entry["action"] == "revoke" for entry in delivery.audit("B1"))


def test_revoke_records_a_clawback_for_claimed_items():
    delivery = pbd.ParcelBatchDelivery()
    delivery.plan("B1", [single_entry()], now=0.0)
    delivery.dispatch("B1", now=1.0)
    mail = delivery.inbox("alice")[0]
    delivery.claim(mail["mail_id"], items=["gold"], now=2.0)
    delivery.revoke("B1", actor="gm", now=3.0)
    revoked = delivery.inbox("alice", include_deleted=True)[0]
    assert revoked["status"] == "revoked"
    ledger = delivery.ledger("alice")
    assert any(entry["kind"] == "claim" and entry["item"] == "gold"
               for entry in ledger)
    clawbacks = [entry for entry in ledger if entry["kind"] == "revoke_claimed"]
    assert clawbacks
    assert clawbacks[0]["item"] == "gold"
    assert clawbacks[0]["qty"] == -100
