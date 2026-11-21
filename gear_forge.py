"""Gear enhancement forge."""

import copy
import random

DEFAULT_CONFIG = {
    "success_rate": 0.4,
    "pity_limit": 3,
    "cost": {"ore": 1},
    "stat_per_level": {"attack": 10},
}


class GearForge:
    def __init__(self, gears=None, inventory=None, config=None, roll=None, state=None):
        self.gears = dict(gears or {})
        self.inventory = dict(inventory or {})
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.roll = roll
        self._versions = {}
        self._version = 0
        if state is not None:
            self.restore(state)

    def _stats(self, level):
        return {name: per * level
                for name, per in self.config["stat_per_level"].items()}

    def _gear(self, gear_id):
        if gear_id not in self.gears:
            raise ValueError("unknown gear: %s" % gear_id)
        return self.gears[gear_id]

    def preview(self, gear_id):
        gear = self._gear(gear_id)
        return {
            "level": gear["level"],
            "success_rate": self.config["success_rate"],
            "cost": dict(self.config["cost"]),
            "stats": self._stats(gear["level"]),
            "next_stats": self._stats(gear["level"] + 1),
            "pity": gear.get("pity", 0),
            "bound": gear.get("bound", False),
        }

    def forge(self, gear_id, version, material="ore"):
        gear = self._gear(gear_id)
        expected = self._versions.get(gear_id, 0)
        if version != expected:
            raise ValueError("stale version: expected %d, got %d"
                             % (expected, version))
        if gear["level"] >= gear.get("max_level", gear["level"]):
            raise ValueError("gear already at max level")
        cost = dict(self.config["cost"])
        for mat, amount in cost.items():
            if self.inventory.get(mat, 0) < amount:
                raise ValueError("insufficient material: %s" % mat)

        rate = self.config["success_rate"]
        forced = gear.get("pity", 0) >= self.config["pity_limit"]
        roll = self.roll() if self.roll is not None else random.random()
        success = forced or roll < rate

        for mat, amount in cost.items():
            self.inventory[mat] -= amount

        charm_used = False
        if success:
            gear["level"] += 1
            gear["pity"] = 0
        else:
            gear["pity"] = gear.get("pity", 0) + 1
            if gear.get("protected"):
                gear["protected"] = False
                charm_used = True
            else:
                gear["level"] = max(0, gear["level"] - 1)

        self._versions[gear_id] = expected + 1
        self._version += 1
        return {
            "success": success,
            "rate": rate,
            "level": gear["level"],
            "pity": gear["pity"],
            "materials_used": cost,
            "stats": self._stats(gear["level"]),
            "bound": gear.get("bound", False),
            "charm_used": charm_used,
        }

    def protect(self, gear_id):
        gear = self._gear(gear_id)
        if gear.get("protected"):
            raise ValueError("gear already protected")
        if self.inventory.get("charm", 0) < 1:
            raise ValueError("no protection charm in inventory")
        self.inventory["charm"] -= 1
        gear["protected"] = True
        self._version += 1
        return {"protected": True}

    def transfer(self, source_id, target_id, version):
        source = self._gear(source_id)
        target = self._gear(target_id)
        expected = self._versions.get(source_id, 0)
        if version != expected:
            raise ValueError("stale version: expected %d, got %d"
                             % (expected, version))
        if target["level"] > 0:
            raise ValueError("target gear already has a level")
        target["level"] = source["level"]
        target["pity"] = source.get("pity", 0)
        source["level"] = 0
        source["pity"] = 0
        self._versions[source_id] = expected + 1
        self._versions[target_id] = self._versions.get(target_id, 0) + 1
        self._version += 1
        return {
            "level": target["level"],
            "source_level": source["level"],
            "target_level": target["level"],
        }

    def snapshot(self):
        return {
            "gears": copy.deepcopy(self.gears),
            "inventory": dict(self.inventory),
            "versions": dict(self._versions),
            "version": self._version,
        }

    def restore(self, state):
        if state.get("version", 0) < self._version:
            raise ValueError("cannot restore an older snapshot")
        self.gears = copy.deepcopy(state["gears"])
        self.inventory = dict(state["inventory"])
        self._versions = dict(state.get("versions", {}))
        self._version = state.get("version", 0)
