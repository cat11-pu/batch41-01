import threading


class SeatLedger:
    """Server-authoritative seat ledger for a single game region."""

    def __init__(self, region, capacity=10):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.region = region
        self.capacity = capacity
        self._rooms = {}
        self._next_room_number = 1
        self._lock = threading.RLock()

    def create_room(self, host_id, capacity=None, now=0.0):
        if capacity is None:
            capacity = self.capacity
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be a positive integer")

        with self._lock:
            room_id = "{}:{:06d}".format(self.region, self._next_room_number)
            self._next_room_number += 1
            room = {
                "id": room_id,
                "capacity": capacity,
                "active": True,
                "host": host_id,
                "members": {},
                "order": [],
                "previous_seats": {},
                "locks": {},
                "countdown": None,
                "next_join_order": 1,
            }
            member = self._new_member(host_id, None, "a", False, 1)
            member["joined_order"] = 0
            room["members"][host_id] = member
            room["order"].append(host_id)
            self._rooms[room_id] = room
            return room_id

    def join(self, room_id, player_id, connection_id=None, team="a",
             spectator=False, seat=None, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            self._release_expired_locks_locked(room, now)

            existing = room["members"].get(player_id)
            if existing is not None:
                if connection_id is not None:
                    existing["connection_id"] = connection_id
                return self._member_info(room, existing)

            if not spectator:
                self._validate_team(team)
                if self._seated_count(room) >= room["capacity"]:
                    raise ValueError("room is full")
            else:
                team = None

            selected_seat = None
            if not spectator:
                selected_seat = self._allocate_seat(room, player_id, seat, now)

            member = self._new_member(
                player_id, connection_id, team, bool(spectator), selected_seat
            )
            member["joined_order"] = room["next_join_order"]
            room["next_join_order"] += 1
            room["members"][player_id] = member
            room["order"].append(player_id)

            if not spectator and room["host"] is None:
                room["host"] = player_id

            if selected_seat is not None:
                lock = room["locks"].get(selected_seat)
                if lock is not None and lock["holder"] == player_id:
                    room["locks"].pop(selected_seat, None)

            if not spectator:
                self._invalidate_countdown(room)
            return self._member_info(room, member)

    def leave(self, room_id, player_id, now=0.0):
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                raise KeyError(room_id)
            if not room["active"]:
                return {
                    "left": False,
                    "player_id": player_id,
                    "was_host": False,
                    "new_host": None,
                    "seat": None,
                }

            member = room["members"].get(player_id)
            if member is None:
                self._release_locks_held_by(room, player_id)
                return {
                    "left": False,
                    "player_id": player_id,
                    "was_host": False,
                    "new_host": room["host"],
                    "seat": None,
                }

            was_host = room["host"] == player_id
            released_seat = member["seat"]
            room["members"].pop(player_id, None)
            if player_id in room["order"]:
                room["order"].remove(player_id)
            if released_seat is not None:
                room["previous_seats"][player_id] = released_seat
            self._release_locks_held_by(room, player_id)

            new_host = room["host"]
            if was_host:
                new_host = self._next_host(room, exclude=player_id)
                room["host"] = new_host

            if not member["spectator"]:
                self._invalidate_countdown(room)
            if not room["members"]:
                room["active"] = False
                room["countdown"] = None
                room["locks"].clear()

            return {
                "left": True,
                "player_id": player_id,
                "was_host": was_host,
                "new_host": new_host,
                "seat": released_seat,
            }

    def set_ready(self, room_id, player_id, ready, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            member = self._get_member(room, player_id)
            if member["spectator"]:
                raise ValueError("spectators cannot ready up")

            ready = bool(ready)
            if member["ready"] != ready:
                member["ready"] = ready
                self._invalidate_countdown(room)
            return self._member_info(room, member)

    def ready_state(self, room_id):
        with self._lock:
            room = self._get_active_room(room_id)
            return {
                player_id: room["members"][player_id]["ready"]
                for player_id in room["order"]
                if not room["members"][player_id]["spectator"]
            }

    def members(self, room_id):
        with self._lock:
            room = self._get_active_room(room_id)
            return [
                self._member_info(room, room["members"][player_id])
                for player_id in room["order"]
            ]

    def connection_of(self, room_id, player_id):
        with self._lock:
            room = self._get_active_room(room_id)
            return self._get_member(room, player_id)["connection_id"]

    def team_counts(self, room_id):
        with self._lock:
            room = self._get_active_room(room_id)
            return self._team_counts(room)

    def change_team(self, room_id, player_id, team, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            member = self._get_member(room, player_id)
            if member["spectator"]:
                raise ValueError("spectators cannot change teams")
            self._validate_team(team)

            if member["team"] != team:
                member["team"] = team
                member["ready"] = False
                self._invalidate_countdown(room)
            return self._team_counts(room)

    def start_countdown(self, room_id, seconds, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                raise ValueError("countdown length must be positive")
            if seconds <= 0:
                raise ValueError("countdown length must be positive")

            countdown = room["countdown"]
            if countdown is not None and (
                countdown["state"] == "started"
                or (countdown["state"] == "pending" and now < countdown["ends_at"])
            ):
                raise ValueError("a countdown is already running")
            if not self._can_start(room):
                raise ValueError("all players must be ready with even teams")

            room["countdown"] = {
                "state": "pending",
                "started_at": now,
                "ends_at": now + seconds,
            }
            return {
                "state": "pending",
                "started_at": now,
                "ends_at": now + seconds,
            }

    def tick(self, room_id, now):
        with self._lock:
            room = self._get_active_room(room_id)
            countdown = room["countdown"]
            if countdown is None:
                return "idle"

            state = countdown["state"]
            if state == "cancelled":
                return "cancelled"
            if state == "started":
                return "started"
            if not self._can_start(room):
                countdown["state"] = "cancelled"
                return "cancelled"
            if now < countdown["ends_at"]:
                return "pending"

            countdown["state"] = "started"
            return "started"

    def cancel_countdown(self, room_id, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            countdown = room["countdown"]
            if countdown is None or countdown["state"] != "pending":
                return False
            countdown["state"] = "cancelled"
            return True

    def dissolve(self, room_id, now=0.0):
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None or not room["active"]:
                return False
            room["active"] = False
            room["countdown"] = None
            room["locks"].clear()
            room["members"].clear()
            room["order"].clear()
            return True

    def active_rooms(self):
        with self._lock:
            return [
                room_id
                for room_id, room in self._rooms.items()
                if room["active"]
            ]

    def reconnect(self, room_id, player_id, connection_id, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            member = self._get_member(room, player_id)
            member["connection_id"] = connection_id
            return self._member_info(room, member)

    def transfer_host(self, room_id, new_host=None, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            old_host = room["host"]
            if new_host is None:
                selected = self._next_host(room, exclude=old_host)
            else:
                member = self._get_member(room, new_host)
                if member["spectator"]:
                    raise ValueError("a spectator cannot become host")
                selected = new_host

            room["host"] = selected
            return {"old_host": old_host, "new_host": selected}

    def join_with_invite(self, room_id, player_id, token, connection_id=None,
                         now=0.0):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("invalid invite token")
        with self._lock:
            self._get_active_room(room_id)
            return self.join(
                room_id, player_id, connection_id=connection_id, now=now
            )

    def lock_seat(self, room_id, seat, holder, now=0.0, ttl=30.0):
        with self._lock:
            room = self._get_active_room(room_id)
            self._validate_seat(room, seat)
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl < 0:
                raise ValueError("ttl must be non-negative")
            if holder is None:
                raise ValueError("lock holder is required")

            occupant = self._member_at_seat(room, seat)
            if occupant is not None and occupant["player_id"] != holder:
                return False

            existing = room["locks"].get(seat)
            if existing is not None:
                if now < existing["expires_at"] and existing["holder"] != holder:
                    return False
                if now < existing["expires_at"] and existing["holder"] == holder:
                    existing["expires_at"] = now + ttl
                    return True
                room["locks"].pop(seat, None)

            room["locks"][seat] = {
                "holder": holder,
                "expires_at": now + ttl,
            }
            return True

    def release_expired_locks(self, room_id, now=0.0):
        with self._lock:
            room = self._get_active_room(room_id)
            return self._release_expired_locks_locked(room, now)

    def seat_of(self, room_id, player_id):
        with self._lock:
            room = self._get_active_room(room_id)
            return self._get_member(room, player_id)["seat"]

    def _new_member(self, player_id, connection_id, team, spectator, seat):
        return {
            "player_id": player_id,
            "connection_id": connection_id,
            "team": team,
            "spectator": spectator,
            "seat": seat,
            "ready": False,
            "joined_order": None,
        }

    def _member_info(self, room, member):
        return {
            "player_id": member["player_id"],
            "connection_id": member["connection_id"],
            "team": member["team"],
            "spectator": member["spectator"],
            "seat": member["seat"],
            "ready": member["ready"],
            "host": room["host"] == member["player_id"],
        }

    def _get_active_room(self, room_id):
        room = self._rooms.get(room_id)
        if room is None or not room["active"]:
            raise KeyError(room_id)
        return room

    def _get_member(self, room, player_id):
        member = room["members"].get(player_id)
        if member is None:
            raise KeyError(player_id)
        return member

    def _validate_team(self, team):
        if team not in ("a", "b"):
            raise ValueError("team must be 'a' or 'b'")

    def _validate_seat(self, room, seat):
        if (
            not isinstance(seat, int)
            or isinstance(seat, bool)
            or seat < 1
            or seat > room["capacity"]
        ):
            raise ValueError("seat is outside the room capacity")

    def _seated_count(self, room):
        return sum(
            1 for member in room["members"].values()
            if not member["spectator"]
        )

    def _member_at_seat(self, room, seat):
        for member in room["members"].values():
            if member["seat"] == seat:
                return member
        return None

    def _allocate_seat(self, room, player_id, requested_seat, now):
        if requested_seat is not None:
            self._validate_seat(room, requested_seat)
            if self._member_at_seat(room, requested_seat) is not None:
                raise ValueError("seat is already occupied")
            if self._seat_locked_for_other(room, requested_seat, player_id, now):
                raise ValueError("seat is locked")
            return requested_seat

        preferred = room["previous_seats"].get(player_id)
        candidate_seats = []
        if preferred is not None and 1 <= preferred <= room["capacity"]:
            candidate_seats.append(preferred)
        candidate_seats.extend(
            seat for seat in range(1, room["capacity"] + 1)
            if seat != preferred
        )

        for seat in candidate_seats:
            if self._member_at_seat(room, seat) is not None:
                continue
            if self._seat_locked_for_other(room, seat, player_id, now):
                continue
            return seat
        raise ValueError("no free seat available")

    def _seat_locked_for_other(self, room, seat, player_id, now):
        lock = room["locks"].get(seat)
        if lock is None:
            return False
        if now >= lock["expires_at"]:
            room["locks"].pop(seat, None)
            return False
        return lock["holder"] != player_id

    def _release_expired_locks_locked(self, room, now):
        expired = [
            seat for seat, lock in room["locks"].items()
            if now >= lock["expires_at"]
        ]
        for seat in expired:
            room["locks"].pop(seat, None)
        return sorted(expired)

    def _release_locks_held_by(self, room, player_id):
        for seat_number, lock in list(room["locks"].items()):
            if lock["holder"] == player_id:
                room["locks"].pop(seat_number, None)

    def _next_host(self, room, exclude=None):
        for player_id in room["order"]:
            if player_id == exclude:
                continue
            member = room["members"].get(player_id)
            if member is not None and not member["spectator"]:
                return player_id
        return None

    def _team_counts(self, room):
        counts = {"a": 0, "b": 0}
        for member in room["members"].values():
            if not member["spectator"]:
                counts[member["team"]] += 1
        return counts

    def _can_start(self, room):
        seated = [
            member for member in room["members"].values()
            if not member["spectator"]
        ]
        if len(seated) < 2:
            return False
        if any(not member["ready"] for member in seated):
            return False
        counts = self._team_counts(room)
        return counts["a"] > 0 and counts["a"] == counts["b"]

    def _invalidate_countdown(self, room):
        countdown = room["countdown"]
        if countdown is not None and countdown["state"] == "pending":
            countdown["state"] = "cancelled"
