"""Dynamic world-line handoff.

Contract used by the tests
--------------------------
* ``spawn(line, entity_id, ...)`` places one entity.  Entity ids are globally
  unique; a duplicate id raises ``ValueError``.  ``kind`` is "player", "pet",
  "summon" or "boss"; pets and summons carry ``owner``.  Spawning beyond
  ``capacity`` raises ``ValueError``.
* ``transfer_out(source, entity_id, target, now)`` validates the request and
  returns ``{"token", ...}`` while the entity stays on the source;
  ``transfer_in(token)`` commits the move atomically under the global lock
  (the whole pet/summon group must fit the target capacity, otherwise
  ``ValueError`` and nothing changes).  ``transfer(...)`` is the convenience
  pair of both.  A transfer keeps ``cooldown_until`` as an absolute time,
  keeps item ids (an item is in exactly one line), updates the quest tracker
  and re-subscribes the guild channel.
* ``fail_transfer(token)`` discards a pending transfer and leaves the entity
  on the source with identical state.
* ``population(line)`` reflects spawns/transfers immediately.
* ``aoi(line, center, radius)`` returns the ids inside the 2D radius around
  ``center`` using each entity's spawn position.
* ``attack(line, entity_id)`` returns True when the entity is on that line and
  raises ``ValueError`` otherwise (one entity cannot be attacked on two lines).
* ``add_aggro`` / ``aggro_targets`` store aggro on the entity, so transferring
  a boss moves its aggro list.
* ``mailbox(account)`` returns ``{"messages": [...], "attachments": n}`` and is
  not changed by a transfer.
* ``record_damage`` stores damage per line.  ``merge(source, target)`` freezes
  the damage snapshot of every existing boss before merging, so
  ``award(boss)`` keeps returning the pre-merge leader.
* ``channels(entity_id)`` returns ``{"line", "subscriptions"}``.
* ``quest_tracker(entity_id)`` returns ``{"line", "x", "y"}``.
* ``logout(entity_id)`` raises ``ValueError`` while a transfer is pending and
  otherwise returns True.
"""

import uuid


class _Entity:
    def __init__(self, entity_id, line, kind, owner, cooldown_until, items,
                 quest_line, guild, x, y):
        self.entity_id = entity_id
        self.line = line
        self.kind = kind
        self.owner = owner
        self.cooldown_until = cooldown_until
        self.items = tuple(items)
        self.quest_line = line if quest_line is None else quest_line
        self.guild = guild
        self.x = x
        self.y = y
        self.aggro = []


class WorldLineHandoff:
    def __init__(self, capacity=100):
        self.capacity = capacity
        self._entities = {}
        self._lines = {}
        self._items = {}
        self._mailboxes = {}
        self._damage = {}
        self._award_snapshots = {}
        self._pending = {}
        self._locked = set()

    def _require_entity(self, entity_id):
        record = self._entities.get(entity_id)
        if record is None:
            raise ValueError(f"unknown entity: {entity_id}")
        return record

    def _transfer_group(self, line, entity_id):
        group = [entity_id]
        for member_id in self._lines.get(line, {}):
            member = self._entities[member_id]
            if member.owner == entity_id:
                group.append(member_id)
        return group

    def _current_leader(self, boss_id):
        totals = {}
        for (line, boss), players in self._damage.items():
            if boss != boss_id:
                continue
            for player_id, amount in players.items():
                totals[player_id] = totals.get(player_id, 0) + amount
        if not totals:
            return None
        return max(totals, key=lambda player_id: totals[player_id])

    def spawn(self, line, entity_id, kind="player", owner=None, cooldown_until=0.0,
              items=(), quest_line=None, guild=False, mail=(), x=0.0, y=0.0):
        if entity_id in self._entities:
            raise ValueError(f"duplicate entity id: {entity_id}")
        line_entities = self._lines.setdefault(line, {})
        if len(line_entities) + 1 > self.capacity:
            raise ValueError(f"line {line} is full")
        for item_id in items:
            if item_id in self._items:
                raise ValueError(f"duplicate item id: {item_id}")
        record = _Entity(entity_id, line, kind, owner, cooldown_until, items,
                         quest_line, guild, x, y)
        self._entities[entity_id] = record
        line_entities[entity_id] = None
        for item_id in record.items:
            self._items[item_id] = (line, entity_id)
        self._mailboxes[entity_id] = {
            "messages": list(mail),
            "attachments": len(mail),
        }
        return record.line

    def transfer(self, source_line, entity_id, target_line, now=0.0):
        granted = self.transfer_out(source_line, entity_id, target_line, now)
        return self.transfer_in(granted["token"])

    def transfer_out(self, source_line, entity_id, target_line, now=0.0):
        record = self._require_entity(entity_id)
        if record.line != source_line:
            raise ValueError(f"entity {entity_id} is not on line {source_line}")
        if source_line == target_line:
            raise ValueError("source and target line must differ")
        if entity_id in self._locked:
            raise ValueError(f"entity {entity_id} already has a pending transfer")
        group = self._transfer_group(source_line, entity_id)
        target_population = len(self._lines.get(target_line, {}))
        if target_population + len(group) > self.capacity:
            raise ValueError(f"line {target_line} cannot fit the transfer group")
        token = uuid.uuid4().hex
        self._pending[token] = {
            "source": source_line,
            "target": target_line,
            "entity": entity_id,
            "group": group,
            "created_at": now,
        }
        self._locked.update(group)
        return {
            "token": token,
            "source": source_line,
            "target": target_line,
            "entity": entity_id,
        }

    def transfer_in(self, token):
        pending = self._pending.get(token)
        if pending is None:
            raise ValueError("unknown or already consumed transfer token")
        source_line = pending["source"]
        target_line = pending["target"]
        group = pending["group"]
        target_entities = self._lines.setdefault(target_line, {})
        if len(target_entities) + len(group) > self.capacity:
            raise ValueError(f"line {target_line} cannot fit the transfer group")
        source_entities = self._lines[source_line]
        for member_id in group:
            member = self._entities[member_id]
            del source_entities[member_id]
            target_entities[member_id] = None
            member.line = target_line
            member.quest_line = target_line
            for item_id in member.items:
                self._items[item_id] = (target_line, member_id)
        self._locked.difference_update(group)
        del self._pending[token]
        return {"entity": pending["entity"], "line": target_line, "group": list(group)}

    def fail_transfer(self, token):
        pending = self._pending.pop(token, None)
        if pending is None:
            raise ValueError("unknown or already consumed transfer token")
        self._locked.difference_update(pending["group"])
        return {"entity": pending["entity"], "line": pending["source"]}

    def aoi(self, line, center, radius):
        center_x, center_y = center
        radius_squared = radius * radius
        visible = []
        for entity_id in self._lines.get(line, {}):
            record = self._entities[entity_id]
            delta_x = record.x - center_x
            delta_y = record.y - center_y
            if delta_x * delta_x + delta_y * delta_y <= radius_squared:
                visible.append(entity_id)
        return visible

    def population(self, line):
        return len(self._lines.get(line, {}))

    def entities(self, line):
        return list(self._lines.get(line, {}))

    def attack(self, line, entity_id):
        record = self._require_entity(entity_id)
        if record.line != line:
            raise ValueError(f"entity {entity_id} is not on line {line}")
        return True

    def add_aggro(self, line, boss_id, target_id):
        record = self._require_entity(boss_id)
        if record.line != line:
            raise ValueError(f"boss {boss_id} is not on line {line}")
        if target_id not in record.aggro:
            record.aggro.append(target_id)

    def aggro_targets(self, line, boss_id):
        record = self._entities.get(boss_id)
        if record is None or record.line != line:
            return []
        return list(record.aggro)

    def mailbox(self, account):
        mailbox = self._mailboxes.get(account)
        if mailbox is None:
            return {"messages": [], "attachments": 0}
        return {"messages": list(mailbox["messages"]),
                "attachments": mailbox["attachments"]}

    def record_damage(self, line, boss_id, player_id, amount):
        board = self._damage.setdefault((line, boss_id), {})
        board[player_id] = board.get(player_id, 0) + amount

    def award(self, boss_id):
        if boss_id in self._award_snapshots:
            return self._award_snapshots[boss_id]
        return self._current_leader(boss_id)

    def merge(self, source_line, target_line):
        if source_line == target_line:
            raise ValueError("source and target line must differ")
        for boss_id in {boss for (_, boss) in self._damage}:
            if boss_id not in self._award_snapshots:
                leader = self._current_leader(boss_id)
                if leader is not None:
                    self._award_snapshots[boss_id] = leader
        source_entities = self._lines.get(source_line, {})
        target_entities = self._lines.setdefault(target_line, {})
        for member_id in list(source_entities):
            record = self._entities[member_id]
            del source_entities[member_id]
            target_entities[member_id] = None
            record.line = target_line
            record.quest_line = target_line
            for item_id in record.items:
                self._items[item_id] = (target_line, member_id)
        return {"merged_from": source_line, "into": target_line}

    def recover(self, entity_id):
        record = self._require_entity(entity_id)
        return {
            "entity_id": record.entity_id,
            "line": record.line,
            "kind": record.kind,
            "owner": record.owner,
            "cooldown_until": record.cooldown_until,
            "items": tuple(record.items),
            "quest_line": record.quest_line,
            "guild": record.guild,
            "x": record.x,
            "y": record.y,
        }

    def logout(self, entity_id):
        self._require_entity(entity_id)
        if entity_id in self._locked:
            raise ValueError("cannot logout while a transfer is pending")
        return True

    def item_lines(self):
        return {item_id: line for item_id, (line, holder) in self._items.items()}

    def channels(self, entity_id):
        record = self._require_entity(entity_id)
        subscriptions = ["guild"] if record.guild else []
        return {"line": record.line, "subscriptions": subscriptions}

    def quest_tracker(self, entity_id):
        record = self._require_entity(entity_id)
        return {"line": record.quest_line, "x": record.x, "y": record.y}
