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
        # Single source of truth for intimacy, keyed by unordered pair.
        self._friends = {}
        self._daily = {}
        self._keys = set()
        self._claims = set()
        self._blocked = set()
        self._removed = set()
        self._event_multiplier = 1.0
        self._server = {}
        self._version = {}

    @staticmethod
    def _pair(a, b):
        return tuple(sorted((a, b)))

    @staticmethod
    def _num(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    def add_friend(self, owner, friend):
        pair = self._pair(owner, friend)
        if pair not in self._friends:
            self._friends[pair] = {"intimacy": 0, "last_decay": self.start_day}

    def remove_friend(self, owner, friend):
        pair = self._pair(owner, friend)
        self._friends.pop(pair, None)
        self._daily = {k: v for k, v in self._daily.items() if k[0] != pair}
        self._keys = {k for k in self._keys if k[0] != pair}
        # Claim history survives removal; leave a tombstone marker.
        self._removed.add(pair)

    def gain(self, owner, friend, amount, day, key=None, source="chat", group_token=None, item_bonus=0.0):
        pair = self._pair(owner, friend)
        if pair not in self._friends:
            raise ValueError("not friends: %r <-> %r" % (owner, friend))
        if pair in self._blocked:
            raise PermissionError("friendship is blocked: %r <-> %r" % (owner, friend))
        if key is not None and (pair, day, key) in self._keys:
            return {"applied": 0, "duplicate": True, "clamped": False}

        base = amount
        if source == "group" and group_token is not None:
            base = amount + 10 * group_token["online"]
        applied = base * self._event_multiplier + base * item_bonus

        daily_key = (pair, day)
        used = self._daily.get(daily_key, 0)
        remaining = self.DAILY_CAP - used
        if remaining <= 0:
            raise ValueError("daily cap reached for %r <-> %r on day %r" % (owner, friend, day))
        clamped = applied > remaining
        if clamped:
            applied = remaining
        applied = self._num(applied)

        self._daily[daily_key] = self._num(used + applied)
        self._friends[pair]["intimacy"] = self._num(self._friends[pair]["intimacy"] + applied)
        if key is not None:
            self._keys.add((pair, day, key))
        return {"applied": applied, "duplicate": False, "clamped": clamped}

    def join_group(self, owner, friend, online):
        return {"owner": owner, "friend": friend, "online": online}

    def set_event_multiplier(self, multiplier):
        self._event_multiplier = multiplier

    def claim(self, owner, friend, tier):
        pair = self._pair(owner, friend)
        if pair not in self._friends:
            raise ValueError("not friends: %r <-> %r" % (owner, friend))
        if (pair, tier) in self._claims:
            raise ValueError("tier %r already claimed for %r <-> %r" % (tier, owner, friend))
        self._claims.add((pair, tier))
        return {"claimed": True, "tier": tier}

    def block(self, owner, friend):
        pair = self._pair(owner, friend)
        self._blocked.add(pair)
        return {"blocked": True}

    def decay(self, owner, to_day):
        decayed = 0
        for pair, state in self._friends.items():
            if owner not in pair:
                continue
            elapsed = to_day - state["last_decay"]
            if elapsed <= 0:
                continue
            loss = min(state["intimacy"], elapsed * self.DECAY_PER_DAY)
            state["intimacy"] = self._num(state["intimacy"] - loss)
            state["last_decay"] = to_day
            decayed = self._num(decayed + loss)
        return {"owner": owner, "to_day": to_day, "decayed": decayed}

    def transfer(self, owner, server):
        version = self._version.get(owner, 0) + 1
        self._version[owner] = version
        self._server[owner] = server
        return {"server": server, "version": version, "intimacy_total": self._intimacy_total(owner)}

    def _intimacy_total(self, owner):
        return self._num(sum(
            state["intimacy"]
            for pair, state in self._friends.items()
            if owner in pair
        ))

    def detail(self, owner, friend):
        pair = self._pair(owner, friend)
        state = self._friends.get(pair)
        intimacy = state["intimacy"] if state else 0
        return {
            "friend": friend,
            "intimacy": intimacy,
            "level": int(intimacy) // 100,
            "blocked": pair in self._blocked,
        }

    def board(self, owner, limit=10):
        entries = []
        for pair, state in self._friends.items():
            if owner not in pair:
                continue
            friend = pair[0] if pair[1] == owner else pair[1]
            intimacy = state["intimacy"]
            entries.append({
                "friend": friend,
                "intimacy": intimacy,
                "level": int(intimacy) // 100,
            })
        entries.sort(key=lambda entry: (-entry["intimacy"], entry["friend"]))
        return entries[:limit]

    def server_view(self, owner, server):
        return {
            "server": server,
            "version": self._version.get(owner, 0),
            "intimacy_total": self._intimacy_total(owner),
            "friends": self.board(owner, limit=len(self._friends) or 1),
        }
