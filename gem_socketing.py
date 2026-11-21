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


class GemSocketing:
    MAX_BONUS = 60
    UNSOCKET_FEE = 50

    def __init__(self, gold=1000):
        self.gold = gold

    def add_gem(self, gem_id, color, level=1, bound=False):
        raise NotImplementedError

    def add_item(self, item_id, sockets, level=1):
        raise NotImplementedError

    def socket(self, item_id, index, gem_id):
        raise NotImplementedError

    def unsocket(self, item_id, index):
        raise NotImplementedError

    def merge(self, gem_ids):
        raise NotImplementedError

    def preview(self, item_id):
        raise NotImplementedError

    def item_state(self, item_id):
        raise NotImplementedError

    def upgrade_item(self, item_id, level):
        raise NotImplementedError

    def salvage(self, item_id):
        raise NotImplementedError

    def sync(self, item_id, server):
        raise NotImplementedError

    def gem_state(self, gem_id):
        raise NotImplementedError

    def balance(self):
        raise NotImplementedError
