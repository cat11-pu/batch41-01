import copy
import ipaddress
import threading
from collections.abc import Mapping


class BestPathArbiter:
    """BGP best path selection with loop, policy and withdrawal handling."""

    DEFAULT_LOCAL_PREF = 100

    def __init__(self, local_as=65000, router_id='192.0.2.1', max_prefixes=1000):
        self.local_as = int(local_as)
        self.router_id = str(router_id)
        self.max_prefixes = (
            None if max_prefixes is None else int(max_prefixes))
        self._lock = threading.RLock()
        self._peers = {}
        self._rib = {}
        self._refresh_pending = {}
        self._refresh_seen = {}
        self.peers = self._peers
        self.rib = self._rib

    def add_peer(self, peer, remote_as, address=None):
        with self._lock:
            peer = str(peer)
            remote_as = int(remote_as)
            previous = self._peers.get(peer)
            if previous is not None and previous["remote_as"] != remote_as:
                self._remove_peer_routes(peer)
            self._peers[peer] = {
                "peer": peer,
                "remote_as": remote_as,
                "address": address,
                "state": "Established",
            }
            return copy.deepcopy(self._peers[peer])

    def peer_state(self, peer):
        with self._lock:
            entry = self._peers.get(str(peer))
            return entry["state"] if entry is not None else "Idle"

    def path_length(self, as_path, as_set=None, confed_path=None):
        sequence, set_groups, _ = self._parse_path(as_path, as_set, confed_path)
        return len(sequence) + len(set_groups)

    def _shut_peer(self, peer):
        with self._lock:
            peer = str(peer)
            removed = self._remove_peer_routes(peer)
            if peer in self._peers:
                self._peers[peer]["state"] = "Idle"
            self._refresh_pending.pop(peer, None)
            self._refresh_seen.pop(peer, None)
            return removed

    def update(self, message):
        with self._lock:
            if not isinstance(message, Mapping):
                return {"accepted": False, "reason": "invalid message"}

            peer = message.get("peer")
            prefix = message.get("prefix")
            if peer is None or prefix is None:
                return {"accepted": False, "reason": "invalid message"}

            peer = str(peer)
            peer_entry = self._peers.get(peer)
            if peer_entry is None:
                return {"accepted": False, "reason": "unknown peer"}
            if peer_entry["state"] != "Established":
                return {"accepted": False, "reason": "peer down"}

            try:
                prefix = self._canonical_prefix(prefix)
                sequence, set_groups, confed_path = self._parse_path(
                    message.get("as_path", ()),
                    message.get("as_set"),
                    message.get("confed_path", message.get("as_confed_path")),
                )
                local_pref = self._as_int(
                    message.get("local_pref", self.DEFAULT_LOCAL_PREF))
                med = self._as_int(message.get("med", 0))
            except (TypeError, ValueError):
                return {"accepted": False, "reason": "invalid attribute"}

            if med < 0 or local_pref < 0:
                return {"accepted": False, "reason": "invalid attribute"}

            all_asns = list(sequence)
            for group in set_groups:
                all_asns.extend(group)
            all_asns.extend(confed_path)

            if self.local_as in all_asns:
                return {"accepted": False, "reason": "as loop"}
            if 0 in all_asns:
                return {"accepted": False, "reason": "as0"}

            existing = self._rib.get(prefix, {})
            is_new_prefix = peer not in existing
            prefix_count = sum(
                1 for routes in self._rib.values() if peer in routes)
            if (is_new_prefix and self.max_prefixes is not None
                    and int(self.max_prefixes) > 0
                    and prefix_count >= int(self.max_prefixes)):
                self._shut_peer(peer)
                return {"accepted": False, "reason": "max prefix exceeded"}

            remote_as = peer_entry["remote_as"]
            if "ebgp" in message and message["ebgp"] is not None:
                ebgp = bool(message["ebgp"])
            else:
                ebgp = remote_as != self.local_as

            advertised_next_hop = message.get("next_hop")
            if ebgp and peer_entry.get("address") is not None:
                next_hop = peer_entry["address"]
            elif advertised_next_hop is not None:
                next_hop = advertised_next_hop
            else:
                next_hop = peer_entry.get("address")

            flattened_set = []
            for group in set_groups:
                flattened_set.extend(group)

            route = {
                "prefix": prefix,
                "peer": peer,
                "neighbor_as": remote_as,
                "remote_as": remote_as,
                "peer_address": peer_entry.get("address"),
                "as_path": list(sequence),
                "as_set": flattened_set,
                "confed_path": list(confed_path),
                "length": len(sequence) + len(set_groups),
                "local_pref": local_pref,
                "med": med,
                "origin": message.get("origin", 0),
                "ebgp": ebgp,
                "next_hop": next_hop,
                "router_id": message.get("router_id")
                or peer_entry.get("address") or peer,
                "stale": False,
                "stale_deadline": None,
            }

            self._rib.setdefault(prefix, {})[peer] = route
            if peer in self._refresh_seen:
                self._refresh_seen[peer].add(prefix)
            return {"accepted": True, "prefix": prefix, "peer": peer}

    def withdraw(self, prefix, peer=None):
        with self._lock:
            if isinstance(prefix, Mapping):
                message = prefix
                prefix = message.get("prefix")
                peer = message.get("peer", peer)
            if prefix is None:
                return []
            try:
                prefix = self._canonical_prefix(prefix)
            except (TypeError, ValueError):
                return []

            routes = self._rib.get(prefix)
            if not routes:
                return []

            if peer is None:
                targets = sorted(routes)
            else:
                peer = str(peer)
                targets = [peer] if peer in routes else []

            removed = []
            for target in targets:
                if target in routes:
                    del routes[target]
                    removed.append((prefix, target))
                    if target in self._refresh_seen:
                        self._refresh_seen[target].discard(prefix)
            if not routes:
                self._rib.pop(prefix, None)
            return removed

    def select(self, candidates):
        with self._lock:
            routes = self._normalize_candidates(candidates)
            best = self._choose_best(routes)
            return self._public_route(best) if best is not None else None

    def best_path(self, prefix):
        with self._lock:
            try:
                prefix = self._canonical_prefix(prefix)
            except (TypeError, ValueError):
                return None
            routes = list(self._rib.get(prefix, {}).values())
            return self.select(routes)

    def fib(self):
        with self._lock:
            table = {}
            for prefix in sorted(self._rib):
                routes = list(self._rib[prefix].values())
                best = self.select(routes)
                if best is not None:
                    table[prefix] = best
            return table

    def route_refresh(self, peer):
        with self._lock:
            peer = str(peer)
            prefixes = {
                prefix for prefix, routes in self._rib.items()
                if peer in routes
            }
            self._refresh_pending[peer] = set(prefixes)
            self._refresh_seen[peer] = set()
            return {"peer": peer, "prefixes": sorted(prefixes)}

    def finish_refresh(self, peer, kept_prefixes):
        with self._lock:
            peer = str(peer)
            pending = self._refresh_pending.pop(peer, None)
            seen = self._refresh_seen.pop(peer, set())

            if kept_prefixes is None:
                kept = set(seen)
            else:
                if isinstance(kept_prefixes, (str, bytes)):
                    kept_prefixes = [kept_prefixes]
                kept = set()
                for prefix in kept_prefixes:
                    try:
                        kept.add(self._canonical_prefix(prefix))
                    except (TypeError, ValueError):
                        continue
                kept.update(seen)

            if pending is None:
                pending = {
                    prefix for prefix, routes in self._rib.items()
                    if peer in routes
                }

            to_withdraw = []
            for prefix in sorted(pending - kept):
                routes = self._rib.get(prefix)
                if routes and peer in routes:
                    del routes[peer]
                    to_withdraw.append(prefix)
                    if not routes:
                        self._rib.pop(prefix, None)

            for prefix in kept:
                route = self._rib.get(prefix, {}).get(peer)
                if route is not None:
                    route["stale"] = False
                    route["stale_deadline"] = None

            return {"withdrawn": to_withdraw}

    def mark_stale(self, peer, deadline):
        with self._lock:
            peer = str(peer)
            marked = []
            for prefix in sorted(self._rib):
                route = self._rib[prefix].get(peer)
                if route is not None:
                    route["stale"] = True
                    route["stale_deadline"] = deadline
                    marked.append((prefix, peer))
            return marked

    def expire_stale(self, now):
        with self._lock:
            removed = []
            for prefix in sorted(list(self._rib)):
                routes = self._rib.get(prefix)
                if not routes:
                    continue
                for peer in sorted(list(routes)):
                    route = routes[peer]
                    if not route.get("stale"):
                        continue
                    deadline = route.get("stale_deadline")
                    if deadline is None:
                        continue
                    try:
                        expired = deadline <= now
                    except TypeError:
                        expired = False
                    if expired:
                        del routes[peer]
                        removed.append((prefix, peer))
                if not routes:
                    self._rib.pop(prefix, None)
            return removed

    def multipath(self, candidates):
        with self._lock:
            routes = self._normalize_candidates(candidates)
            best = self._choose_best(routes)
            if best is None:
                return []

            best_key = self._equivalence_key(best, best)
            equal = [
                route for route in routes
                if self._equivalence_key(route, best) == best_key
            ]
            equal.sort(key=self._stable_key)
            return [self._public_route(route) for route in equal]

    def apply(self, events):
        with self._lock:
            if isinstance(events, Mapping):
                events = [events]
            results = []
            for event in events:
                if not isinstance(event, Mapping):
                    results.append(
                        {"accepted": False, "reason": "invalid event"})
                    continue
                action = str(event.get("action", "update")).lower()
                if action in {"withdraw", "withdrawal"}:
                    results.append(
                        self.withdraw(event.get("prefix"), event.get("peer")))
                elif action in {"update", "announce"}:
                    results.append(self.update(event))
                else:
                    results.append(
                        {"accepted": False, "reason": "unknown action"})
            return results

    def snapshot(self):
        with self._lock:
            peers = {}
            for peer in sorted(self._peers):
                entry = copy.deepcopy(self._peers[peer])
                entry["prefix_count"] = sum(
                    1 for routes in self._rib.values() if peer in routes)
                peers[peer] = entry

            ribs = {}
            for prefix in sorted(self._rib):
                ribs[prefix] = {
                    peer: copy.deepcopy(self._rib[prefix][peer])
                    for peer in sorted(self._rib[prefix])
                }

            return {
                "local_as": self.local_as,
                "router_id": self.router_id,
                "max_prefixes": self.max_prefixes,
                "peers": peers,
                "rib": ribs,
                "routes": copy.deepcopy(ribs),
                "fib": self.fib(),
            }

    def _remove_peer_routes(self, peer):
        removed = []
        for prefix in sorted(list(self._rib)):
            routes = self._rib.get(prefix)
            if routes and peer in routes:
                del routes[peer]
                removed.append((prefix, peer))
                if not routes:
                    self._rib.pop(prefix, None)
        if peer in self._refresh_seen:
            self._refresh_seen[peer].clear()
        return removed

    @staticmethod
    def _as_int(value):
        if isinstance(value, bool):
            raise ValueError("boolean is not an AS number")
        if isinstance(value, str):
            value = value.strip()
            if value.upper().startswith("AS"):
                value = value[2:]
        number = int(value, 10) if isinstance(value, str) else int(value)
        return number

    @classmethod
    def _parse_path(cls, as_path, as_set=None, confed_path=None):
        sequence = []
        set_groups = []
        confederation = []

        def kind_of(segment):
            kind = segment.get(
                "type", segment.get(
                    "segment_type", segment.get("kind", "sequence")))
            return str(kind).upper().replace("-", "_")

        def payload_of(segment):
            return segment.get(
                "value", segment.get(
                    "asns", segment.get(
                        "as_path", segment.get("as_set", ()))))

        def append_sequence(value):
            if value is None:
                return
            if isinstance(value, Mapping):
                if "CONFED" in kind_of(value):
                    confederation.extend(cls._asns(payload_of(value)))
                elif "SET" in kind_of(value):
                    group = cls._asns(payload_of(value))
                    if group:
                        set_groups.append(group)
                else:
                    sequence.extend(cls._asns(payload_of(value)))
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    append_sequence(item)
                return
            sequence.extend(cls._asns(value))

        def append_group(value):
            if value is None:
                return
            if isinstance(value, Mapping):
                if "CONFED" in kind_of(value):
                    confederation.extend(cls._asns(payload_of(value)))
                else:
                    group = cls._asns(payload_of(value))
                    if group:
                        set_groups.append(group)
                return
            group = cls._asns(value)
            if group:
                set_groups.append(group)

        def append_confederation(value):
            if value is None:
                return
            if isinstance(value, Mapping):
                confederation.extend(cls._asns(payload_of(value)))
                return
            confederation.extend(cls._asns(value))

        append_sequence(as_path)
        append_group(as_set)
        append_confederation(confed_path)
        return sequence, set_groups, confederation

    @classmethod
    def _asns(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{") and text.endswith("}"):
                text = text[1:-1]
            parts = text.replace(",", " ").split()
            return [cls._as_int(part) for part in parts]
        if isinstance(value, (list, tuple, set, frozenset)):
            result = []
            for item in value:
                result.extend(cls._asns(item))
            return result
        return [cls._as_int(value)]

    @staticmethod
    def _canonical_prefix(prefix):
        if not isinstance(prefix, str):
            raise TypeError("prefix must be a string")
        return str(ipaddress.ip_network(prefix.strip(), strict=False))

    @staticmethod
    def _ip_sort_key(value):
        if value is None:
            return (9, 0)
        try:
            address = ipaddress.ip_address(str(value))
            return (address.version, int(address))
        except ValueError:
            return (8, str(value))

    @classmethod
    def _origin_key(cls, value):
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        names = {
            "IGP": 0, "I": 0,
            "EGP": 1, "E": 1,
            "INCOMPLETE": 2, "?": 2,
        }
        return names.get(str(value).upper(), 2)

    def _neighbor_as(self, route):
        for key in ("neighbor_as", "remote_as", "peer_as"):
            if route.get(key) is not None:
                return int(route[key])
        peer = route.get("peer")
        entry = self._peers.get(str(peer)) if peer is not None else None
        if entry is not None:
            return entry["remote_as"]
        as_path = route.get("as_path")
        if isinstance(as_path, (list, tuple)) and as_path:
            first = as_path[0]
            if not isinstance(first, Mapping):
                try:
                    return self._as_int(first)
                except (TypeError, ValueError):
                    pass
        return None

    def _is_ebgp(self, route):
        if route.get("ebgp") is not None:
            return bool(route["ebgp"])
        neighbor_as = self._neighbor_as(route)
        return neighbor_as is not None and neighbor_as != self.local_as

    def _normalize_candidates(self, candidates):
        if candidates is None:
            return []
        if isinstance(candidates, Mapping):
            candidates = candidates.values()
        routes = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            route = dict(candidate)
            try:
                sequence, set_groups, confed_path = self._parse_path(
                    route.get("as_path", ()),
                    route.get("as_set"),
                    route.get("confed_path", route.get("as_confed_path")),
                )
                has_path_fields = (
                    route.get("as_path") is not None
                    or route.get("as_set") is not None
                    or route.get("confed_path") is not None
                )
                if has_path_fields or route.get("length") is None:
                    route["length"] = len(sequence) + len(set_groups)
                else:
                    route["length"] = int(route["length"])
                route["local_pref"] = int(route.get(
                    "local_pref", self.DEFAULT_LOCAL_PREF))
                route["med"] = int(route.get("med", 0))
            except (TypeError, ValueError):
                continue
            route.setdefault("origin", 0)
            route["_neighbor_as"] = self._neighbor_as(route)
            route["_ebgp"] = self._is_ebgp(route)
            routes.append(route)
        return routes

    def _stable_key(self, route):
        peer = route.get("peer")
        address = route.get("peer_address")
        if address is None and peer is not None:
            entry = self._peers.get(str(peer))
            if entry is not None:
                address = entry.get("address")
        router_id = route.get("router_id")
        return (
            self._ip_sort_key(router_id),
            self._ip_sort_key(address),
            str(peer) if peer is not None else "",
        )

    @staticmethod
    def _public_route(route):
        return copy.deepcopy({
            key: value for key, value in route.items()
            if not key.startswith("_")
        })

    def _choose_best(self, routes):
        if not routes:
            return None

        highest_pref = max(route["local_pref"] for route in routes)
        survivors = [
            route for route in routes
            if route["local_pref"] == highest_pref
        ]

        shortest = min(int(route["length"]) for route in survivors)
        survivors = [
            route for route in survivors
            if int(route["length"]) == shortest
        ]

        best_origin = min(
            self._origin_key(route.get("origin")) for route in survivors)
        survivors = [
            route for route in survivors
            if self._origin_key(route.get("origin")) == best_origin
        ]

        neighbor_groups = {}
        for route in survivors:
            neighbor_as = route["_neighbor_as"]
            group_key = (
                ("as", neighbor_as) if neighbor_as is not None
                else ("peer", str(route.get("peer")))
            )
            neighbor_groups.setdefault(group_key, []).append(route)

        med_survivors = []
        for group in neighbor_groups.values():
            lowest_med = min(int(route["med"]) for route in group)
            med_survivors.extend(
                route for route in group
                if int(route["med"]) == lowest_med
            )
        survivors = med_survivors

        if any(route["_ebgp"] for route in survivors):
            survivors = [
                route for route in survivors if route["_ebgp"]
            ]

        return min(survivors, key=self._stable_key)

    def _equivalence_key(self, route, reference):
        route_as = route["_neighbor_as"]
        reference_as = reference["_neighbor_as"]
        if route_as is not None and route_as == reference_as:
            med = int(route["med"])
        else:
            med = 0
        return (
            route["local_pref"],
            int(route["length"]),
            self._origin_key(route.get("origin")),
            med,
            route["_ebgp"],
        )
