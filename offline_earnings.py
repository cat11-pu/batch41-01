"""Offline earnings settlement with mail and claim records."""

import copy

DEFAULT_CONFIG = {
    "rate": 100,
    "cap_hours": 8,
    "mail_ttl": 72,
    "maintenance": [],
}


class OfflineEarnings:
    def __init__(self, config=None, state=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.state = state if state is not None else {"players": {}}

    def _players(self):
        return self.state.setdefault("players", {})

    def _player(self, player_id):
        return self._players().setdefault(player_id, {})

    def _tick(self, player, now):
        last_clock = player.get("last_clock")
        if last_clock is not None and now < last_clock:
            raise ValueError("clock rollback is not allowed")
        player["last_clock"] = now

    def mark_offline(self, player_id, at, team=None, guild=None,
                     guild_bonus=0.0, double=False):
        player = self._player(player_id)
        self._tick(player, at)
        player["logout_at"] = at
        player["logout_snapshot"] = {
            "team": list(team) if team is not None else [],
            "guild": guild,
            "guild_bonus": guild_bonus,
            "double": double,
        }
        player.setdefault("current_state", {
            "team": list(team) if team is not None else [],
            "guild": guild,
        })
        player.setdefault("settlements", [])
        player.setdefault("claim_keys", {})

    def settle(self, player_id, now):
        player = self._players().get(player_id)
        if player is None or "logout_at" not in player:
            raise ValueError("player has no offline record")
        self._tick(player, now)

        logout_at = player["logout_at"]
        settlements = player.setdefault("settlements", [])
        for record in settlements:
            if record["logout_at"] == logout_at:
                return self._settle_result(record)

        raw_hours = now - logout_at
        deducted = 0
        for start, end in self.config["maintenance"]:
            overlap = min(now, end) - max(logout_at, start)
            if overlap > 0:
                deducted += overlap

        effective_hours = max(0, raw_hours - deducted)
        cap_hours = self.config["cap_hours"]
        hours = min(effective_hours, cap_hours)

        snap = player["logout_snapshot"]
        multiplier = 1.0 + snap["guild_bonus"]
        if snap["double"]:
            multiplier *= 2.0

        rate = self.config["rate"]
        base_amount = int(round(hours * rate))
        amount = int(round(base_amount * multiplier))

        seq = player.get("seq", 0) + 1
        player["seq"] = seq
        record = {
            "window_id": "{}:{}:{}".format(player_id, logout_at, seq),
            "logout_at": logout_at,
            "settled_at": now,
            "raw_hours": raw_hours,
            "maintenance_deducted": deducted,
            "hours": hours,
            "rate": rate,
            "multiplier": multiplier,
            "base_amount": base_amount,
            "amount": amount,
            "team": list(snap["team"]),
            "guild": snap["guild"],
            "detail": {
                "rate": rate,
                "raw_hours": raw_hours,
                "maintenance_deducted": deducted,
                "capped": effective_hours > cap_hours,
                "multiplier": multiplier,
                "base_amount": base_amount,
            },
            "expires_at": now + self.config["mail_ttl"],
            "claimed": False,
            "claimed_at": None,
            "claim_key": None,
            "total_credited": 0,
        }
        settlements.append(record)
        return self._settle_result(record)

    def _settle_result(self, record):
        return {
            "window_id": record["window_id"],
            "logout_at": record["logout_at"],
            "settled_at": record["settled_at"],
            "hours": record["hours"],
            "amount": record["amount"],
            "team": list(record["team"]),
            "guild": record["guild"],
            "detail": dict(record["detail"]),
            "expires_at": record["expires_at"],
        }

    def claim(self, player_id, now=0, bag_space=999, claim_key=None):
        player = self._players().get(player_id)
        if player is None or not player.get("settlements"):
            raise ValueError("no settled earnings to claim")
        self._tick(player, now)

        settlements = player["settlements"]
        claim_keys = player.setdefault("claim_keys", {})
        if claim_key is not None and claim_key in claim_keys:
            return self._duplicate_result(settlements[claim_keys[claim_key]])

        record = settlements[-1]
        if record["claimed"]:
            return self._duplicate_result(record)
        if now > record["expires_at"]:
            raise ValueError("earnings mail has expired")

        items = {"gold": record["amount"]}
        if bag_space < len(items):
            raise ValueError("not enough bag space")

        record["claimed"] = True
        record["claimed_at"] = now
        record["claim_key"] = claim_key
        record["total_credited"] = record["amount"]
        if claim_key is not None:
            claim_keys[claim_key] = len(settlements) - 1

        return {
            "window_id": record["window_id"],
            "amount": record["amount"],
            "duplicate": False,
            "total_credited": record["total_credited"],
            "items": dict(items),
            "claimed_at": now,
        }

    def _duplicate_result(self, record):
        return {
            "window_id": record["window_id"],
            "amount": 0,
            "duplicate": True,
            "total_credited": record["total_credited"],
            "items": {},
            "claimed_at": record["claimed_at"],
        }

    def expire(self, player_id, now):
        player = self._players().get(player_id)
        if player is None or not player.get("settlements"):
            raise ValueError("no settled earnings to expire")
        self._tick(player, now)

        record = player["settlements"][-1]
        expired = now > record["expires_at"]
        if expired:
            record["expired"] = True
        return {
            "window_id": record["window_id"],
            "issued_at": record["settled_at"],
            "expires_at": record["expires_at"],
            "expired": expired,
        }

    def snapshot(self):
        return copy.deepcopy(self.state)

    def update_online_state(self, player_id, team=None, guild=None):
        player = self._player(player_id)
        current = player.setdefault("current_state", {})
        if team is not None:
            current["team"] = team
        if guild is not None:
            current["guild"] = guild
