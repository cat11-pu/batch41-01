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
        pass

    def create_room(self, team_id, server):
        raise NotImplementedError

    def join(self, room_id, account, device="pc"):
        raise NotImplementedError

    def leave(self, room_id, account):
        raise NotImplementedError

    def mute(self, room_id, account, muted):
        raise NotImplementedError

    def kick(self, room_id, actor, target):
        raise NotImplementedError

    def transfer(self, room_id, actor, target):
        raise NotImplementedError

    def set_online(self, room_id, account, online):
        raise NotImplementedError

    def reconnect(self, room_id, account, device="pc"):
        raise NotImplementedError

    def audible(self, room_id, listener):
        raise NotImplementedError

    def switch_channel(self, room_id, account, channel):
        raise NotImplementedError

    def block(self, listener, speaker):
        raise NotImplementedError

    def close(self, room_id):
        raise NotImplementedError

    def room_state(self, room_id):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
