"""Multi-colour gem socketing service.

Contract used by the tests
--------------------------
* A gem has ``color``, ``level`` (>= 1) and ``bound``.  Its attack value is
  ``10 * level``.
* An item has an ordered list of socket colours and a level.
* ``socket`` succeeds only when the socket colour equals the gem colour, the
  socket is empty and the gem is in the bag.  On success the gem leaves the
  bag and lives in exactly one socket globally.
* ``unsocket`` costs ``UNSOCKET_FEE = 50`` gold, charged only on success; the
  gem returns to the bag.  Without enough gold it raises ``ValueError`` and
  changes nothing.
* ``merge`` consumes exactly three gems of the same colour and level and
  produces one gem of level + 1; the result is bound when any input was bound.
* ``preview(item_id)`` returns ``{"sockets", "bonus"}`` and must equal the
  socket part of ``item_state(item_id)``.
* The bonus is the sum of socketed gems' attack, capped at ``MAX_BONUS = 60``
  per item.
* ``upgrade_item`` keeps sockets and socketed gems.
* ``salvage`` returns every socketed gem to the bag and clears the sockets.
* ``sync(item_id, server)`` returns ``{"server", "version", "gems"}``; the
  version starts at 1 and increments on each sync.
* Concurrent ``socket`` calls on one socket: exactly one wins.
"""

import threading


class GemSocketing:
    MAX_BONUS = 60
    UNSOCKET_FEE = 50

    def __init__(self, gold=1000):
        self.gold = gold
        self._lock = threading.RLock()
        self._gems = {}
        self._items = {}
        self._merge_seq = 0
        self._sync_versions = {}

    def add_gem(self, gem_id, color, level=1, bound=False):
        with self._lock:
            if gem_id in self._gems:
                raise ValueError("gem already exists: %s" % gem_id)
            if level < 1:
                raise ValueError("gem level must be >= 1")
            self._gems[gem_id] = {
                "id": gem_id,
                "color": color,
                "level": level,
                "bound": bool(bound),
                "in_bag": True,
            }

    def add_item(self, item_id, sockets, level=1):
        with self._lock:
            if item_id in self._items:
                raise ValueError("item already exists: %s" % item_id)
            self._items[item_id] = {
                "id": item_id,
                "level": level,
                "sockets": list(sockets),
                "gems": [None] * len(sockets),
            }

    def _get_item(self, item_id):
        item = self._items.get(item_id)
        if item is None:
            raise ValueError("unknown item: %s" % item_id)
        return item

    def _get_gem(self, gem_id):
        gem = self._gems.get(gem_id)
        if gem is None:
            raise ValueError("unknown gem: %s" % gem_id)
        return gem

    def _socket_view(self, item):
        return [
            {"index": index, "color": color, "gem": item["gems"][index]}
            for index, color in enumerate(item["sockets"])
        ]

    def _bonus(self, item):
        total = sum(
            10 * self._gems[gem_id]["level"]
            for gem_id in item["gems"]
            if gem_id is not None
        )
        return min(total, self.MAX_BONUS)

    def socket(self, item_id, index, gem_id):
        with self._lock:
            item = self._get_item(item_id)
            gem = self._get_gem(gem_id)
            if not 0 <= index < len(item["sockets"]):
                raise ValueError("invalid socket index: %s" % index)
            if item["gems"][index] is not None:
                raise ValueError("socket already occupied")
            if not gem["in_bag"]:
                raise ValueError("gem is not in the bag")
            if item["sockets"][index] != gem["color"]:
                raise ValueError("gem colour does not match socket")
            item["gems"][index] = gem_id
            gem["in_bag"] = False
            return {"item": item_id, "index": index, "gem": gem_id}

    def unsocket(self, item_id, index):
        with self._lock:
            item = self._get_item(item_id)
            if not 0 <= index < len(item["sockets"]):
                raise ValueError("invalid socket index: %s" % index)
            gem_id = item["gems"][index]
            if gem_id is None:
                raise ValueError("socket is empty")
            if self.gold < self.UNSOCKET_FEE:
                raise ValueError("not enough gold to unsocket")
            self.gold -= self.UNSOCKET_FEE
            item["gems"][index] = None
            self._gems[gem_id]["in_bag"] = True
            return {"item": item_id, "index": index, "gem": gem_id,
                    "fee": self.UNSOCKET_FEE}

    def merge(self, gem_ids):
        with self._lock:
            if len(gem_ids) != 3:
                raise ValueError("merge requires exactly three gems")
            gems = [self._get_gem(gem_id) for gem_id in gem_ids]
            if len(set(gem_ids)) != 3:
                raise ValueError("merge requires three distinct gems")
            color = gems[0]["color"]
            level = gems[0]["level"]
            for gem in gems:
                if not gem["in_bag"]:
                    raise ValueError("gem is not in the bag")
                if gem["color"] != color or gem["level"] != level:
                    raise ValueError("gems must share colour and level")
            bound = any(gem["bound"] for gem in gems)
            for gem in gems:
                gem["in_bag"] = False
            self._merge_seq += 1
            new_id = "merged_%d" % self._merge_seq
            self._gems[new_id] = {
                "id": new_id,
                "color": color,
                "level": level + 1,
                "bound": bound,
                "in_bag": True,
            }
            return {"gem": dict(self._gems[new_id])}

    def preview(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            return {"sockets": self._socket_view(item),
                    "bonus": self._bonus(item)}

    def item_state(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            return {"id": item_id,
                    "level": item["level"],
                    "sockets": self._socket_view(item),
                    "bonus": self._bonus(item)}

    def upgrade_item(self, item_id, level):
        with self._lock:
            item = self._get_item(item_id)
            item["level"] = level
            return self.item_state(item_id)

    def salvage(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            returned = []
            for index, gem_id in enumerate(item["gems"]):
                if gem_id is not None:
                    self._gems[gem_id]["in_bag"] = True
                    returned.append(gem_id)
                    item["gems"][index] = None
            return {"item": item_id, "gems": returned}

    def sync(self, item_id, server):
        with self._lock:
            item = self._get_item(item_id)
            version = self._sync_versions.get(item_id, 0) + 1
            self._sync_versions[item_id] = version
            gems = [gem_id for gem_id in item["gems"] if gem_id is not None]
            return {"server": server, "version": version, "gems": gems}

    def gem_state(self, gem_id):
        with self._lock:
            return dict(self._get_gem(gem_id))

    def balance(self):
        with self._lock:
            return self.gold
