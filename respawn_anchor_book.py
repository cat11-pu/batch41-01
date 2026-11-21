class RespawnAnchor:
    """Per-instance respawn anchors, boss progress and loot snapshots."""

    def __init__(self):
        self._instances = {}

    def create_instance(self, instance_id, dungeon_id, team_id,
                        difficulty="normal", entrance=(0, 0),
                        walkable=None, anchors=None, bosses=None):
        walkable = set(walkable) if walkable else {entrance}
        walkable.add(entrance)
        anchors = dict(anchors) if anchors else {}
        bosses = dict(bosses) if bosses else {}
        entities = {}
        for boss_id, trash_ids in bosses.items():
            entities[boss_id] = "alive"
            for trash_id in trash_ids:
                entities[trash_id] = "alive"
        self._instances[(instance_id, team_id, difficulty)] = {
            "instance_id": instance_id,
            "dungeon_id": dungeon_id,
            "team_id": team_id,
            "difficulty": difficulty,
            "entrance": entrance,
            "walkable": walkable,
            "anchors": anchors,
            "bosses": bosses,
            "entities": entities,
            "current_anchor": "entrance",
            "bindings": {},
            "loot": {},
            "leader": None,
            "wipe_count": 0,
            "wipe_timer": None,
        }

    def _lookup(self, instance_id):
        matches = [inst for key, inst in self._instances.items()
                   if key[0] == instance_id]
        if not matches:
            raise KeyError(instance_id)
        if len(matches) > 1:
            raise KeyError("ambiguous instance_id: %r" % (instance_id,))
        return matches[0]

    def _anchor_info(self, inst, anchor_id):
        if anchor_id == "entrance":
            position = inst["entrance"]
        else:
            position = inst["anchors"][anchor_id]
        return {"anchor_id": anchor_id, "position": position}

    def _validate_anchor(self, inst, anchor_id):
        if anchor_id == "entrance":
            return
        if anchor_id not in inst["anchors"]:
            raise ValueError("unknown anchor: %r" % (anchor_id,))
        if inst["anchors"][anchor_id] not in inst["walkable"]:
            raise ValueError("anchor %r is not on walkable ground" % (anchor_id,))

    def activate(self, instance_id, anchor_id, actor=None):
        inst = self._lookup(instance_id)
        self._validate_anchor(inst, anchor_id)
        inst["current_anchor"] = anchor_id
        return self._anchor_info(inst, anchor_id)

    def current_anchor(self, instance_id):
        inst = self._lookup(instance_id)
        return self._anchor_info(inst, inst["current_anchor"])

    def bind(self, instance_id, player_id, now=0.0):
        inst = self._lookup(instance_id)
        inst["bindings"][player_id] = inst["current_anchor"]
        return self._anchor_info(inst, inst["bindings"][player_id])

    def respawn(self, instance_id, player_id, now=0.0):
        inst = self._lookup(instance_id)
        anchor_id = inst["bindings"].get(player_id, inst["current_anchor"])
        self._validate_anchor(inst, anchor_id)
        info = self._anchor_info(inst, anchor_id)
        info["player_id"] = player_id
        return info

    def kill_boss(self, instance_id, boss_id, roster, anchor_id=None, now=0.0):
        inst = self._lookup(instance_id)
        if boss_id not in inst["bosses"]:
            raise ValueError("unknown boss: %r" % (boss_id,))
        if anchor_id is not None:
            self._validate_anchor(inst, anchor_id)
        inst["entities"][boss_id] = "dead"
        for trash_id in inst["bosses"][boss_id]:
            inst["entities"][trash_id] = "dead"
        inst["loot"][boss_id] = set(roster)
        if anchor_id is not None:
            inst["current_anchor"] = anchor_id
        return self._anchor_info(inst, inst["current_anchor"])

    def reset_boss(self, instance_id, boss_id, now=0.0):
        inst = self._lookup(instance_id)
        if boss_id not in inst["bosses"]:
            raise ValueError("unknown boss: %r" % (boss_id,))
        inst["entities"][boss_id] = "alive"
        for trash_id in inst["bosses"][boss_id]:
            inst["entities"][trash_id] = "alive"
        inst["loot"].pop(boss_id, None)

    def state(self, instance_id, entity_id):
        inst = self._lookup(instance_id)
        return inst["entities"].get(entity_id, "alive")

    def reset_instance(self, instance_id, now=0.0):
        inst = self._lookup(instance_id)
        for entity_id in inst["entities"]:
            inst["entities"][entity_id] = "alive"
        inst["current_anchor"] = "entrance"
        inst["bindings"].clear()
        inst["loot"].clear()
        inst["wipe_count"] = 0
        inst["wipe_timer"] = None

    def restore(self, instance_id, player_id):
        inst = self._lookup(instance_id)
        anchor_id = inst["bindings"].get(player_id, inst["current_anchor"])
        info = self._anchor_info(inst, anchor_id)
        info["player_id"] = player_id
        return info

    def join_late(self, instance_id, player_id):
        return self.bind(instance_id, player_id)

    def wipe(self, instance_id, now=0.0):
        inst = self._lookup(instance_id)
        inst["wipe_count"] += 1
        info = self._anchor_info(inst, inst["current_anchor"])
        info["wiped"] = True
        info["wipe_count"] = inst["wipe_count"]
        return info

    def start_wipe_timer(self, instance_id, seconds, now=0.0):
        inst = self._lookup(instance_id)
        inst["wipe_timer"] = {"started_at": now, "ends_at": now + seconds}
        return dict(inst["wipe_timer"])

    def loot_eligible(self, instance_id, boss_id, player_id):
        inst = self._lookup(instance_id)
        return player_id in inst["loot"].get(boss_id, set())

    def change_leader(self, instance_id, new_leader):
        inst = self._lookup(instance_id)
        inst["leader"] = new_leader
        return new_leader
