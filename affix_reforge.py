"""Affix reforge with locked affixes, preview tokens and history."""

import copy
import random
import threading
import uuid


class _ReforgeState:
    """Mutable per-service state shared across entries and server regions."""

    def __init__(self):
        self.locks = {}
        self.tokens = {}
        self.history = {}
        self.attempts = {}
        self.item_locks = {}


class AffixReforge:
    def __init__(self, pool=None, items=None, inventory=None, cost=None,
                 roll=None, state=None):
        self.pool = list(pool or [])
        self.items = dict(items or {})
        self.inventory = dict(inventory or {})
        self.cost = dict(cost or {})
        self.roll = roll or random.random
        self.state = state or _ReforgeState()

    def _item_lock(self, item_id):
        lock = self.state.item_locks.get(item_id)
        if lock is None:
            lock = threading.RLock()
            self.state.item_locks[item_id] = lock
        return lock

    def _get_item(self, item_id):
        item = self.items.get(item_id)
        if item is None:
            raise ValueError("unknown item: %s" % item_id)
        return item

    def _locked_ids(self, item_id):
        return set(self.state.locks.get(item_id, ()))

    def _eligible(self, entry, item):
        if entry["min_level"] > item["level"]:
            return False
        if item["quality"] not in entry.get("qualities", []):
            return False
        return True

    def _weight(self, entry, quality):
        weights = entry.get("weights", {})
        if isinstance(weights, dict):
            return weights.get(quality, weights.get("default", 1))
        return weights or 1

    def _make_affix(self, entry):
        return {
            "id": entry["id"],
            "value": entry["value"],
            "set_id": entry.get("set_id"),
        }

    def _set_bonus(self, item, affixes):
        set_id = item.get("set_id")
        if not set_id:
            return {"set_id": None, "pieces": 0, "active": False}
        pieces = sum(1 for affix in affixes if affix.get("set_id") == set_id)
        return {"set_id": set_id, "pieces": pieces, "active": pieces >= 2}

    def _roll_candidates(self, item_id, item):
        locked = self._locked_ids(item_id)
        open_slots = item["slots"] - len(
            [affix for affix in item["affixes"] if affix["id"] in locked]
        )
        eligible = [
            entry for entry in self.pool
            if entry["id"] not in locked and self._eligible(entry, item)
        ]
        chosen = []
        remaining = list(eligible)
        for _ in range(open_slots):
            if not remaining:
                raise ValueError("not enough eligible affixes for item: %s"
                                 % item_id)
            weights = [self._weight(entry, item["quality"])
                       for entry in remaining]
            total = sum(weights)
            target = self.roll() * total
            upto = 0
            index = len(remaining) - 1
            for index, weight in enumerate(weights):
                upto += weight
                if target < upto:
                    break
            chosen.append(self._make_affix(remaining.pop(index)))
        return chosen

    def preview(self, item_id):
        with self._item_lock(item_id):
            item = self._get_item(item_id)
            candidates = self._roll_candidates(item_id, item)
            token = uuid.uuid4().hex
            self.state.tokens[token] = {
                "item_id": item_id,
                "version": item["version"],
                "candidates": copy.deepcopy(candidates),
                "used": False,
            }
            return {
                "token": token,
                "candidates": candidates,
                "cost": copy.deepcopy(self.cost),
                "version": item["version"],
            }

    def _consume_token(self, item_id, token):
        record = self.state.tokens.get(token)
        if record is None or record["used"]:
            raise ValueError("invalid or reused preview token")
        if record["item_id"] != item_id:
            raise ValueError("token does not belong to item: %s" % item_id)
        item = self._get_item(item_id)
        if record["version"] != item["version"]:
            raise ValueError("stale preview token")
        candidates = copy.deepcopy(record["candidates"])
        locked_count = sum(
            1 for affix in item["affixes"]
            if affix["id"] in self._locked_ids(item_id)
        )
        if locked_count + len(candidates) != item["slots"]:
            raise ValueError("lock set changed since preview")
        return record, item, candidates

    def _check_materials(self):
        for material, amount in self.cost.items():
            if self.inventory.get(material, 0) < amount:
                raise ValueError("not enough materials: %s" % material)

    def reforge(self, item_id, token=None):
        with self._item_lock(item_id):
            record, item, candidates = self._consume_token(item_id, token)
            self._check_materials()

            before = copy.deepcopy(item["affixes"])
            locked = self._locked_ids(item_id)
            rolled = list(candidates)
            merged = []
            for affix in item["affixes"]:
                if affix["id"] in locked:
                    merged.append(copy.deepcopy(affix))
                else:
                    merged.append(rolled.pop(0))
            if rolled:
                raise ValueError("candidate count does not match open slots")

            charged = dict(self.cost)
            for material, amount in charged.items():
                self.inventory[material] = (
                    self.inventory.get(material, 0) - amount
                )
            item["affixes"] = merged
            item["version"] += 1
            record["used"] = True
            for stale in self.state.tokens.values():
                if stale["item_id"] == item_id:
                    stale["used"] = True

            attempt = self.state.attempts.get(item_id, 0) + 1
            self.state.attempts[item_id] = attempt
            entry = {
                "attempt": attempt,
                "before": before,
                "after": copy.deepcopy(merged),
                "cost": copy.deepcopy(charged),
                "version": item["version"],
            }
            self.state.history.setdefault(item_id, []).append(entry)

            return {
                "affixes": copy.deepcopy(merged),
                "materials": copy.deepcopy(self.inventory),
                "cost": copy.deepcopy(charged),
                "version": item["version"],
                "set_bonus": self._set_bonus(item, merged),
            }

    def lock(self, item_id, affix_ids):
        with self._item_lock(item_id):
            item = self._get_item(item_id)
            known = {affix["id"] for affix in item["affixes"]}
            unknown = [affix_id for affix_id in affix_ids
                       if affix_id not in known]
            if unknown:
                raise ValueError("unknown affix on item: %s" % unknown)
            self.state.locks[item_id] = list(affix_ids)
            return list(affix_ids)

    def undo(self, item_id):
        with self._item_lock(item_id):
            item = self._get_item(item_id)
            entries = self.state.history.get(item_id)
            if not entries:
                raise ValueError("no reforge to undo for item: %s" % item_id)
            entry = entries.pop()
            item["affixes"] = copy.deepcopy(entry["before"])
            item["version"] = entry["version"] - 1
            refunded = copy.deepcopy(entry["cost"])
            for material, amount in refunded.items():
                self.inventory[material] = (
                    self.inventory.get(material, 0) + amount
                )
            self.state.attempts[item_id] = (
                self.state.attempts.get(item_id, 1) - 1
            )
            for record in self.state.tokens.values():
                if record["item_id"] == item_id:
                    record["used"] = True
            return {
                "affixes": copy.deepcopy(item["affixes"]),
                "refunded": refunded,
                "materials": copy.deepcopy(self.inventory),
                "version": item["version"],
            }

    def history(self, item_id):
        self._get_item(item_id)
        return copy.deepcopy(self.state.history.get(item_id, []))
