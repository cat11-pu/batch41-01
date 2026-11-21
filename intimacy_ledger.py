"""Friend intimacy ledger.

Contract used by the tests
--------------------------
* Friendship pairs are unordered.  ``gain`` requires an existing friendship.
* ``DAILY_CAP = 200`` per (owner, friend, day).  A gain is clamped to the
  remaining daily budget (``clamped`` is True when clamping happened).  When
  the pair is already at the cap the call raises ``ValueError``.
* ``key`` makes a gain idempotent per (owner, friend, day, key): a repeat
  returns ``{"applied": 0, "duplicate": True}`` and touches nothing.
* For ``source="group"`` with a token from ``join_group``, the base amount is
  ``amount + 10 * token["online"]`` using the snapshot taken at join time.
* Event and item bonuses are separate buckets:
  ``applied = base * event_multiplier + base * item_bonus`` (defaults 1.0/0.0).
* ``claim`` rewards a tier once per friendship forever (across days, friend
  removal/re-add and transfers); a repeat raises ``ValueError``.
* ``remove_friend`` clears intimacy and daily counters but keeps claim
  history; re-adding starts at zero.
* ``block`` makes gains in either direction raise ``PermissionError``.
* ``decay(owner, to_day)`` subtracts 5 intimacy per elapsed world day since
  the pair's last decay, floored at 0; the same target day is a no-op.
* ``transfer`` returns ``{"server", "version", "intimacy_total"}``; the
  version starts at 1 and increments, claim history migrates.
* ``board`` and ``detail`` read the same store; ``level = intimacy // 100``.
"""


class IntimacyLedger:
    DAILY_CAP = 200
    DECAY_PER_DAY = 5

    def __init__(self, start_day=0):
        self.start_day = start_day

    def add_friend(self, owner, friend):
        raise NotImplementedError

    def remove_friend(self, owner, friend):
        raise NotImplementedError

    def gain(self, owner, friend, amount, day, key=None, source="chat", group_token=None, item_bonus=0.0):
        raise NotImplementedError

    def join_group(self, owner, friend, online):
        raise NotImplementedError

    def set_event_multiplier(self, multiplier):
        raise NotImplementedError

    def claim(self, owner, friend, tier):
        raise NotImplementedError

    def block(self, owner, friend):
        raise NotImplementedError

    def decay(self, owner, to_day):
        raise NotImplementedError

    def transfer(self, owner, server):
        raise NotImplementedError

    def detail(self, owner, friend):
        raise NotImplementedError

    def board(self, owner, limit=10):
        raise NotImplementedError

    def server_view(self, owner, server):
        raise NotImplementedError
