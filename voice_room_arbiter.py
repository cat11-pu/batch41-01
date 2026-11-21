"""Team voice room arbiter.

Contract used by the tests
--------------------------
* ``create_room(team_id, server)`` returns a room id unique across servers
  (the same team on two servers gets two different ids).
* ``CAPACITY = 8`` members per room; capacity is checked atomically.
* ``join`` rejects a second device for an account already in the room with
  ``ValueError`` and rejects blacklisted accounts with ``PermissionError``.
  Seats are assigned lowest-free-first; the default channel is "team".
* ``room_state(room_id)`` returns ``{"host", "members", "closed", "resources"}``;
  ``members`` maps account -> {"seat", "muted", "online", "channel"}.
* ``mute`` is the only source of truth for microphones.
* ``kick`` requires the host, frees the seat and blacklists the target for
  the room (rejoin and reconnect raise ``PermissionError``).
* ``transfer`` requires the host and an online member target; otherwise
  ``ValueError``.
* ``reconnect`` restores the member's previous seat and mute flag.
* ``audible(listener)`` lists accounts the listener hears: same channel, not
  muted, not blocked by the listener, not the listener.
* ``switch_channel`` leaves the old channel.
* ``close`` marks the room closed, releases resources and rejects later joins.
* ``stats`` returns ``{"rooms_open", "rooms_closed", "resources"}``.
"""


class VoiceRoomArbiter:
    CAPACITY = 8

    def __init__(self):
        import threading

        self._lock = threading.RLock()
        self._rooms = {}
        self._room_seq = 0
        self._closed_count = 0
        self._blocks = {}

    def create_room(self, team_id, server):
        with self._lock:
            self._room_seq += 1
            room_id = "%s:%s:%d" % (server, team_id, self._room_seq)
            self._rooms[room_id] = {
                "host": None,
                "members": {},
                "blacklist": set(),
                "closed": False,
                "resources": 1,
            }
            return room_id

    def _get_room(self, room_id):
        room = self._rooms.get(room_id)
        if room is None:
            raise ValueError("unknown room: %r" % (room_id,))
        return room

    @staticmethod
    def _lowest_free_seat(room):
        taken = {member["seat"] for member in room["members"].values()}
        seat = 0
        while seat in taken:
            seat += 1
        return seat

    def join(self, room_id, account, device="pc"):
        with self._lock:
            room = self._get_room(room_id)
            if room["closed"]:
                raise ValueError("room is closed")
            if account in room["blacklist"]:
                raise PermissionError("account is blacklisted")
            if account in room["members"]:
                raise ValueError("account already in room")
            if len(room["members"]) >= self.CAPACITY:
                raise ValueError("room is full")
            seat = self._lowest_free_seat(room)
            room["members"][account] = {
                "seat": seat,
                "muted": False,
                "online": True,
                "channel": "team",
                "device": device,
            }
            if room["host"] is None:
                room["host"] = account
            return {"seat": seat, "channel": "team"}

    def leave(self, room_id, account):
        with self._lock:
            room = self._get_room(room_id)
            if account not in room["members"]:
                raise ValueError("account not in room")
            del room["members"][account]
            if room["host"] == account:
                room["host"] = None
                for candidate, info in room["members"].items():
                    if info["online"]:
                        room["host"] = candidate
                        break
                if room["host"] is None and room["members"]:
                    room["host"] = next(iter(room["members"]))

    def mute(self, room_id, account, muted):
        with self._lock:
            room = self._get_room(room_id)
            member = room["members"].get(account)
            if member is None:
                raise ValueError("account not in room")
            member["muted"] = bool(muted)

    def kick(self, room_id, actor, target):
        with self._lock:
            room = self._get_room(room_id)
            if actor != room["host"]:
                raise PermissionError("only the host can kick")
            if target not in room["members"]:
                raise ValueError("target not in room")
            del room["members"][target]
            room["blacklist"].add(target)

    def transfer(self, room_id, actor, target):
        with self._lock:
            room = self._get_room(room_id)
            if actor != room["host"]:
                raise PermissionError("only the host can transfer")
            member = room["members"].get(target)
            if member is None or not member["online"]:
                raise ValueError("target must be an online member")
            room["host"] = target
            return {"host": target}

    def set_online(self, room_id, account, online):
        with self._lock:
            room = self._get_room(room_id)
            member = room["members"].get(account)
            if member is None:
                raise ValueError("account not in room")
            member["online"] = bool(online)

    def reconnect(self, room_id, account, device="pc"):
        with self._lock:
            room = self._get_room(room_id)
            if room["closed"]:
                raise ValueError("room is closed")
            if account in room["blacklist"]:
                raise PermissionError("account is blacklisted")
            member = room["members"].get(account)
            if member is None:
                raise ValueError("account not in room")
            member["online"] = True
            member["device"] = device
            return {"seat": member["seat"], "muted": member["muted"]}

    def audible(self, room_id, listener):
        with self._lock:
            room = self._get_room(room_id)
            member = room["members"].get(listener)
            if member is None:
                raise ValueError("listener not in room")
            blocked = self._blocks.get(listener, set())
            return [
                account
                for account, info in room["members"].items()
                if account != listener
                and info["channel"] == member["channel"]
                and info["online"]
                and not info["muted"]
                and account not in blocked
            ]

    def switch_channel(self, room_id, account, channel):
        with self._lock:
            room = self._get_room(room_id)
            member = room["members"].get(account)
            if member is None:
                raise ValueError("account not in room")
            member["channel"] = channel

    def block(self, listener, speaker):
        with self._lock:
            self._blocks.setdefault(listener, set()).add(speaker)

    def close(self, room_id):
        with self._lock:
            room = self._get_room(room_id)
            if not room["closed"]:
                room["closed"] = True
                room["resources"] = 0
                room["members"].clear()
                room["host"] = None
                self._closed_count += 1
            return {"closed": True}

    def room_state(self, room_id):
        with self._lock:
            room = self._get_room(room_id)
            return {
                "host": room["host"],
                "members": {
                    account: {
                        "seat": info["seat"],
                        "muted": info["muted"],
                        "online": info["online"],
                        "channel": info["channel"],
                    }
                    for account, info in room["members"].items()
                },
                "closed": room["closed"],
                "resources": room["resources"],
            }

    def stats(self):
        with self._lock:
            return {
                "rooms_open": len(self._rooms) - self._closed_count,
                "rooms_closed": self._closed_count,
                "resources": sum(
                    room["resources"] for room in self._rooms.values()
                ),
            }
