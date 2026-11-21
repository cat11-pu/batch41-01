import pytest

import market_mirror_ledger as mml


def published(ledger, listing_id="L1", price=100, server="s1", item="sword"):
    return ledger.publish(listing_id, "seller", item, price, server=server, now=0.0)


def test_publish_starts_at_version_one_with_single_escrow():
    ledger = mml.MarketMirrorLedger()
    result = published(ledger)
    assert result["version"] == 1
    assert result["status"] == "open"
    assert ledger.escrow_count("L1") == 1


def test_duplicate_listing_cannot_be_published_by_another_server():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    with pytest.raises(ValueError):
        ledger.publish("L1", "seller", "sword", 100, server="s2", now=1.0)
    assert ledger.escrow_count("L1") == 1
    assert ledger.cache_get("s1", "L1")["version"] == 1


def test_replicate_is_incremental_by_watermark():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=500.0)
    first = ledger.replicate("s2")
    assert [event["version"] for event in first["events"]] == [1]
    assert first["watermark"] == 1
    ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    second = ledger.replicate("s2", watermark=first["watermark"])
    assert [event["version"] for event in second["events"]] == [2]
    assert second["watermark"] == 2


def test_purchase_validates_expected_version():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=500.0)
    with pytest.raises(ValueError):
        ledger.purchase("L1", "buyer", buyer_server="s2",
                        expected_version=99, now=1.0)
    assert ledger.cache_get("s1", "L1")["status"] == "open"


def test_double_purchase_across_mirrors_spends_only_once():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=500.0)
    ledger.set_player("s1", "other", currency=500.0)
    first = ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    assert first["version"] == 2
    with pytest.raises(ValueError):
        ledger.purchase("L1", "other", buyer_server="s1", now=2.0)
    assert ledger.escrow_count("L1") == 0
    assert ledger.inventory("s1", "buyer").get("sword", 0) == 1
    assert ledger.inventory("s1", "other").get("sword", 0) == 0
    assert ledger.balance("s1", "seller") == pytest.approx(95.0)


def test_cancel_tombstone_is_replicated_and_blocks_cached_purchase():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    first = ledger.replicate("s2")
    cancelled = ledger.cancel("L1", "seller", now=1.0)
    assert cancelled["status"] == "cancelled"
    assert cancelled["tombstone"] is True
    assert cancelled["version"] == 2
    second = ledger.replicate("s2", watermark=first["watermark"])
    assert second["events"][0]["status"] == "cancelled"
    ledger.set_player("s2", "buyer", currency=500.0)
    with pytest.raises(ValueError):
        ledger.purchase("L1", "buyer", buyer_server="s2", now=2.0)
    with pytest.raises(ValueError):
        ledger.cancel("L1", "seller", now=3.0)


def test_purchased_item_routes_to_the_source_server():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=500.0)
    result = ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    assert result["item_server"] == "s1"
    assert ledger.inventory("s1", "buyer").get("sword", 0) == 1
    assert ledger.inventory("s2", "buyer").get("sword", 0) == 0


def test_tax_is_computed_at_the_settlement_server():
    ledger = mml.MarketMirrorLedger(
        settlement_server="s1", tax_rates={"s1": 0.05, "s2": 0.20}
    )
    published(ledger, price=100)
    ledger.set_player("s2", "buyer", currency=500.0)
    result = ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    assert result["tax"] == pytest.approx(5.0)
    assert result["seller_net"] == pytest.approx(95.0)
    assert ledger.balance("s1", "seller") == pytest.approx(95.0)
    assert ledger.balance("s2", "buyer") == pytest.approx(400.0)


def test_replayed_old_open_event_does_not_revive_sold_listing():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=500.0)
    ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    result = ledger.apply_event(
        {"event_id": "e1", "listing_id": "L1", "version": 1, "status": "open"}
    )
    assert result["applied"] is False
    assert ledger.cache_get("s1", "L1")["status"] == "sold"


def test_event_consumption_is_idempotent_by_event_id():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    event = {"event_id": "e2", "listing_id": "L1", "version": 2, "status": "sold"}
    first = ledger.apply_event(event)
    second = ledger.apply_event(event)
    assert first["applied"] is True
    assert second["duplicate"] is True
    cached = ledger.cache_get("s1", "L1")
    assert cached["version"] == 2
    assert cached["status"] == "sold"


def test_reconcile_never_downgrades_a_newer_mirror_version():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.replicate("s2")
    ledger.apply_event(
        {"event_id": "e3", "listing_id": "L1", "version": 3, "status": "cancelled"},
        target_server="s2",
    )
    ledger.reconcile("s2")
    cached = ledger.cache_get("s2", "L1")
    assert cached["version"] == 3
    assert cached["status"] == "cancelled"


def test_mirror_cache_receives_each_new_version():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.replicate("s2")
    assert ledger.cache_get("s2", "L1")["version"] == 1
    assert ledger.cache_get("s2", "L1")["status"] == "open"
    ledger.set_player("s2", "buyer", currency=500.0)
    ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    ledger.replicate("s2", watermark=1)
    assert ledger.cache_get("s2", "L1")["version"] == 2
    assert ledger.cache_get("s2", "L1")["status"] == "sold"


def test_search_deduplicates_listings_and_hides_cancelled_ones():
    ledger = mml.MarketMirrorLedger()
    published(ledger, "L1")
    published(ledger, "L2")
    ledger.replicate("s2")
    ledger.replicate("s2", watermark=1)
    ids = [entry["listing_id"] for entry in ledger.search("s2")]
    assert sorted(ids) == ["L1", "L2"]
    ledger.cancel("L1", "seller", now=1.0)
    ledger.replicate("s2", watermark=1)
    ids = [entry["listing_id"] for entry in ledger.search("s2")]
    assert ids == ["L2"]


def test_insufficient_balance_fails_atomically():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.set_player("s2", "buyer", currency=50.0)
    with pytest.raises(ValueError):
        ledger.purchase("L1", "buyer", buyer_server="s2", now=1.0)
    assert ledger.cache_get("s1", "L1")["status"] == "open"
    assert ledger.escrow_count("L1") == 1
    assert ledger.inventory("s1", "buyer").get("sword", 0) == 0
    assert ledger.balance("s2", "buyer") == pytest.approx(50.0)
    assert ledger.balance("s1", "seller") == pytest.approx(0.0)


def test_lower_version_event_cannot_overwrite_a_newer_one():
    ledger = mml.MarketMirrorLedger()
    published(ledger)
    ledger.replicate("s2")
    ledger.apply_event(
        {"event_id": "e4", "listing_id": "L1", "version": 4, "status": "sold"},
        target_server="s2",
    )
    stale = ledger.apply_event(
        {"event_id": "e5", "listing_id": "L1", "version": 2, "status": "open"},
        target_server="s2",
    )
    assert stale["applied"] is False
    cached = ledger.cache_get("s2", "L1")
    assert cached["version"] == 4
    assert cached["status"] == "sold"
