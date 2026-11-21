class SeatLedger:
    """Server-authoritative seat ledger for a single game region."""

    def __init__(self, region, capacity=10):
        self.region = region
        self.capacity = capacity

    def create_room(self, host_id, capacity=None, now=0.0):
        raise NotImplementedError

    def join(self, room_id, player_id, connection_id=None, team="a",
             spectator=False, seat=None, now=0.0):
        raise NotImplementedError

    def leave(self, room_id, player_id, now=0.0):
        raise NotImplementedError

    def set_ready(self, room_id, player_id, ready, now=0.0):
        raise NotImplementedError

    def ready_state(self, room_id):
        raise NotImplementedError

    def members(self, room_id):
        raise NotImplementedError

    def connection_of(self, room_id, player_id):
        raise NotImplementedError

    def team_counts(self, room_id):
        raise NotImplementedError

    def change_team(self, room_id, player_id, team, now=0.0):
        raise NotImplementedError

    def start_countdown(self, room_id, seconds, now=0.0):
        raise NotImplementedError

    def tick(self, room_id, now):
        raise NotImplementedError

    def cancel_countdown(self, room_id, now=0.0):
        raise NotImplementedError

    def dissolve(self, room_id, now=0.0):
        raise NotImplementedError

    def active_rooms(self):
        raise NotImplementedError

    def reconnect(self, room_id, player_id, connection_id, now=0.0):
        raise NotImplementedError

    def transfer_host(self, room_id, new_host=None, now=0.0):
        raise NotImplementedError

    def join_with_invite(self, room_id, player_id, token, connection_id=None,
                         now=0.0):
        raise NotImplementedError

    def lock_seat(self, room_id, seat, holder, now=0.0, ttl=30.0):
        raise NotImplementedError

    def release_expired_locks(self, room_id, now=0.0):
        raise NotImplementedError

    def seat_of(self, room_id, player_id):
        raise NotImplementedError
