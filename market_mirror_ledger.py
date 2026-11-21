import threading


class MarketMirrorLedger:
    """Cross-server auction mirror with a single source-of-truth writer."""

    def __init__(self, servers=("s1", "s2"), settlement_server="s1",
                 tax_rates=None, home_server=None):
        self.servers = tuple(servers)
        self.settlement_server = settlement_server
        self.tax_rates = dict(tax_rates or {"s1": 0.05, "s2": 0.20})
        self.home_server = home_server

        self._lock = threading.RLock()
        # Source-of-truth listing records. Only the listing's source server
        # ever mutates these; mirrors keep read-only caches.
        self._listings = {}
        # Append-only event log. Every change gets a strictly increasing
        # version, which is also the per-listing version of that change.
        self._events = []
        self._version = 0
        self._listing_locks = {}

        self._cache = {server: {} for server in self.servers}
        self._consumed = {server: set() for server in self.servers}
        self._watermark = {server: 0 for server in self.servers}

        # The escrow ledger is the single source of truth for held items.
        self._escrow = {}
        self._balances = {server: {} for server in self.servers}
        self._inventory = {server: {} for server in self.servers}

    def _bucket(self, table, server):
        bucket = table.get(server)
        if bucket is None:
            bucket = {}
            table[server] = bucket
        return bucket

    def _listing_lock(self, listing_id):
        lock = self._listing_locks.get(listing_id)
        if lock is None:
            lock = threading.RLock()
            self._listing_locks[listing_id] = lock
        return lock

    def _append_event(self, listing_id, status, fields=None):
        self._version += 1
        event = {
            "event_id": f"{listing_id}:v{self._version}",
            "listing_id": listing_id,
            "version": self._version,
            "status": status,
        }
        if fields:
            event.update(fields)
        self._events.append(event)
        return event

    def _apply_to_cache(self, server, event):
        """Apply one event to a mirror cache; stale versions never overwrite."""
        cache = self._bucket(self._cache, server)
        current = cache.get(event["listing_id"])
        if current is not None and event["version"] < current["version"]:
            return False
        snapshot = dict(current or {})
        snapshot.update(event)
        cache[event["listing_id"]] = snapshot
        return True

    def _ingest_event(self, server, event):
        """Idempotent event consumption keyed by event_id on each server."""
        consumed = self._bucket(self._consumed, server)
        if event["event_id"] in consumed:
            return False, True
        applied = self._apply_to_cache(server, event)
        if applied:
            consumed.add(event["event_id"])
        return applied, False

    def _public_view(self, record):
        return {
            "listing_id": record["listing_id"],
            "version": record["version"],
            "status": record["status"],
            "seller": record["seller"],
            "item": record["item"],
            "price": record["price"],
            "server": record["server"],
        }

    def set_player(self, server, player, currency=0.0, items=None):
        with self._lock:
            self._bucket(self._balances, server)[player] = float(currency)
            bag = self._bucket(self._inventory, server).setdefault(player, {})
            for item, quantity in (items or {}).items():
                bag[item] = bag.get(item, 0) + quantity

    def publish(self, listing_id, seller, item, price, server="s1", now=0.0):
        with self._lock:
            if listing_id in self._listings:
                raise ValueError(f"listing {listing_id} already exists")
            with self._listing_lock(listing_id):
                price = float(price)
                record = {
                    "listing_id": listing_id,
                    "seller": seller,
                    "item": item,
                    "price": price,
                    "server": server,
                    "status": "open",
                    "version": 0,
                }
                self._listings[listing_id] = record
                self._escrow[listing_id] = 1
                event = self._append_event(listing_id, "open", {
                    "seller": seller,
                    "item": item,
                    "price": price,
                    "server": server,
                })
                record["version"] = event["version"]
                self._apply_to_cache(server, event)
                return self._public_view(record)

    def replicate(self, server, watermark=0, now=0.0):
        with self._lock:
            events = [
                dict(event)
                for event in self._events
                if event["version"] > watermark
            ]
            for event in events:
                self._ingest_event(server, event)
            self._watermark[server] = max(
                self._watermark.get(server, 0), watermark, self._version
            )
            return {"events": events, "watermark": self._watermark[server]}

    def purchase(self, listing_id, buyer, buyer_server="s2",
                 expected_version=None, now=0.0):
        with self._lock:
            record = self._listings.get(listing_id)
            if record is None:
                raise ValueError(f"listing {listing_id} does not exist")
            with self._listing_lock(listing_id):
                if record["status"] != "open":
                    raise ValueError(
                        f"listing {listing_id} is not open: {record['status']}"
                    )
                if (expected_version is not None
                        and expected_version != record["version"]):
                    raise ValueError(
                        f"version conflict: expected {expected_version}, "
                        f"found {record['version']}"
                    )

                price = record["price"]
                buyer_balances = self._bucket(self._balances, buyer_server)
                funds = buyer_balances.get(buyer, 0.0)
                if funds < price:
                    raise ValueError("insufficient balance")

                rate = self.tax_rates.get(self.settlement_server, 0.0)
                tax = price * rate
                seller_net = price - tax
                source_server = record["server"]

                # All mutations happen after every validation has passed, so
                # a failed purchase leaves balances, escrow and items intact.
                buyer_balances[buyer] = funds - price
                seller_balances = self._bucket(self._balances, source_server)
                seller_balances[record["seller"]] = (
                    seller_balances.get(record["seller"], 0.0) + seller_net
                )
                buyer_bag = self._bucket(self._inventory, source_server).setdefault(
                    buyer, {}
                )
                buyer_bag[record["item"]] = buyer_bag.get(record["item"], 0) + 1
                self._escrow[listing_id] = 0

                record["status"] = "sold"
                event = self._append_event(listing_id, "sold", {
                    "seller": record["seller"],
                    "item": record["item"],
                    "price": price,
                    "server": source_server,
                    "buyer": buyer,
                    "buyer_server": buyer_server,
                    "tax": tax,
                    "seller_net": seller_net,
                    "item_server": source_server,
                })
                record["version"] = event["version"]
                self._apply_to_cache(source_server, event)

                return {
                    "listing_id": listing_id,
                    "version": event["version"],
                    "status": "sold",
                    "buyer": buyer,
                    "buyer_server": buyer_server,
                    "price": price,
                    "tax": tax,
                    "seller_net": seller_net,
                    "item": record["item"],
                    "item_server": source_server,
                }

    def cancel(self, listing_id, actor, now=0.0):
        with self._lock:
            record = self._listings.get(listing_id)
            if record is None:
                raise ValueError(f"listing {listing_id} does not exist")
            with self._listing_lock(listing_id):
                if record["status"] != "open":
                    raise ValueError(
                        f"listing {listing_id} is not open: {record['status']}"
                    )
                if actor != record["seller"]:
                    raise ValueError("only the seller can cancel the listing")

                source_server = record["server"]
                seller_bag = self._bucket(self._inventory, source_server).setdefault(
                    record["seller"], {}
                )
                seller_bag[record["item"]] = seller_bag.get(record["item"], 0) + 1
                self._escrow[listing_id] = 0

                record["status"] = "cancelled"
                event = self._append_event(listing_id, "cancelled", {
                    "seller": record["seller"],
                    "item": record["item"],
                    "price": record["price"],
                    "server": source_server,
                    "tombstone": True,
                })
                record["version"] = event["version"]
                self._apply_to_cache(source_server, event)

                return {
                    "listing_id": listing_id,
                    "version": event["version"],
                    "status": "cancelled",
                    "tombstone": True,
                }

    def reconcile(self, server, now=0.0):
        with self._lock:
            cache = self._bucket(self._cache, server)
            synced = []
            conflicts = []
            for event in self._events:
                current = cache.get(event["listing_id"])
                if current is None or event["version"] > current["version"]:
                    applied, _ = self._ingest_event(server, event)
                    if applied:
                        synced.append(event["listing_id"])
                elif event["version"] < current["version"]:
                    # The mirror holds a newer version; the source log never
                    # downgrades it, the divergence is only reported.
                    conflicts.append(event["listing_id"])
            self._watermark[server] = max(
                self._watermark.get(server, 0), self._version
            )
            return {
                "server": server,
                "synced": synced,
                "conflicts": conflicts,
                "watermark": self._watermark[server],
            }

    def search(self, server, query=""):
        with self._lock:
            cache = self._bucket(self._cache, server)
            entries = {}
            for listing_id, snapshot in cache.items():
                if snapshot.get("status") != "open":
                    continue
                if query and query not in (
                    listing_id, str(snapshot.get("item", ""))
                ):
                    continue
                # Deduplicate by listing id regardless of replay/fan-out.
                entries[listing_id] = dict(snapshot)
            return [entries[key] for key in sorted(entries)]

    def cache_get(self, server, listing_id):
        with self._lock:
            snapshot = self._bucket(self._cache, server).get(listing_id)
            return dict(snapshot) if snapshot is not None else None

    def apply_event(self, event, target_server=None, now=0.0):
        with self._lock:
            listing_id = event["listing_id"]
            if target_server is None:
                record = self._listings.get(listing_id)
                target_server = (
                    record["server"] if record is not None
                    else self.settlement_server
                )
            consumed = self._bucket(self._consumed, target_server)
            if event["event_id"] in consumed:
                return {
                    "applied": False,
                    "duplicate": True,
                    "version": event["version"],
                }
            applied = self._apply_to_cache(target_server, event)
            if applied:
                consumed.add(event["event_id"])
            return {
                "applied": applied,
                "duplicate": False,
                "version": event["version"],
            }

    def escrow_count(self, listing_id):
        with self._lock:
            return self._escrow.get(listing_id, 0)

    def inventory(self, server, player):
        with self._lock:
            return dict(self._bucket(self._inventory, server).get(player, {}))

    def balance(self, server, player):
        with self._lock:
            return float(self._bucket(self._balances, server).get(player, 0.0))
