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


class WorldLineHandoff:
    def __init__(self, capacity=100):
        self.capacity = capacity

    def spawn(self, line, entity_id, kind="player", owner=None, cooldown_until=0.0,
              items=(), quest_line=None, guild=False, mail=(), x=0.0, y=0.0):
        raise NotImplementedError

    def transfer(self, source_line, entity_id, target_line, now=0.0):
        raise NotImplementedError

    def transfer_out(self, source_line, entity_id, target_line, now=0.0):
        raise NotImplementedError

    def transfer_in(self, token):
        raise NotImplementedError

    def fail_transfer(self, token):
        raise NotImplementedError

    def aoi(self, line, center, radius):
        raise NotImplementedError

    def population(self, line):
        raise NotImplementedError

    def entities(self, line):
        raise NotImplementedError

    def attack(self, line, entity_id):
        raise NotImplementedError

    def add_aggro(self, line, boss_id, target_id):
        raise NotImplementedError

    def aggro_targets(self, line, boss_id):
        raise NotImplementedError

    def mailbox(self, account):
        raise NotImplementedError

    def record_damage(self, line, boss_id, player_id, amount):
        raise NotImplementedError

    def award(self, boss_id):
        raise NotImplementedError

    def merge(self, source_line, target_line):
        raise NotImplementedError

    def recover(self, entity_id):
        raise NotImplementedError

    def logout(self, entity_id):
        raise NotImplementedError

    def item_lines(self):
        raise NotImplementedError

    def channels(self, entity_id):
        raise NotImplementedError

    def quest_tracker(self, entity_id):
        raise NotImplementedError
