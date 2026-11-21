"""Ranked promotion series with best-of-three state."""

import copy

DEFAULT_CONFIG = {
    "protection": 100,
    "abandon_cooldown": 50,
    "wins_needed": 2,
    "losses_allowed": 2,
}


class PromotionSeries:
    def __init__(self, config=None, inventory=None, state=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.inventory = dict(inventory or {})
        self.state = copy.deepcopy(state) if state is not None else {}

    def _player(self, player_id):
        player = self.state.get(player_id)
        if player is None:
            raise ValueError("promotion series not started")
        return player

    def _active(self, player_id):
        player = self._player(player_id)
        if player.get("status") != "active":
            raise ValueError("no active promotion series")
        return player

    def _series_view(self, player, duplicate=False):
        return {
            "status": player["status"],
            "wins": player["wins"],
            "losses": player["losses"],
            "rank": player["rank_snapshot"],
            "duplicate": duplicate,
        }

    def start(self, player_id, season, rank, now=0):
        player = self.state.get(player_id)
        if player is not None and now < player.get("cooldown_until", 0):
            raise ValueError("abandon cooldown still active")
        if player is not None and player.get("status") == "active":
            if player.get("season") != season:
                self.settle(player_id, season, now=now)
                player = self.state.get(player_id)
        self.state[player_id] = {
            "season": season,
            "current_rank": rank,
            "rank_snapshot": rank,
            "wins": 0,
            "losses": 0,
            "status": "active",
            "matches": {},
            "opponents": [],
            "protection_until": player.get("protection_until", 0)
            if player else 0,
            "cooldown_until": player.get("cooldown_until", 0)
            if player else 0,
        }
        return self._series_view(self.state[player_id])

    def record(self, player_id, match_id, result, opponent_id=None,
               disconnected=False, now=0):
        player = self._player(player_id)
        existing = player["matches"].get(match_id)
        if existing is not None:
            return self._series_view(player, duplicate=True)
        if player.get("status") != "active":
            raise ValueError("promotion series not active")
        if (opponent_id is not None
                and player["opponents"].count(opponent_id) >= 2):
            raise ValueError("same opponent cannot be matched three times")
        if disconnected:
            result = "loss"
        is_win = result == "win"
        player["matches"][match_id] = {
            "result": "win" if is_win else "loss",
            "opponent_id": opponent_id,
            "disconnected": bool(disconnected),
            "now": now,
        }
        if opponent_id is not None:
            player["opponents"].append(opponent_id)
        if is_win:
            player["wins"] += 1
            if player["wins"] >= self.config["wins_needed"]:
                player["status"] = "ready"
        else:
            player["losses"] += 1
            if player["losses"] >= self.config["losses_allowed"]:
                player["status"] = "failed"
        return self._series_view(player)

    def advance(self, player_id, now=0, use_pass=False):
        player = self._player(player_id)
        if use_pass:
            if self.inventory.get("pass", 0) <= 0:
                raise ValueError("no free pass available")
            self.inventory["pass"] -= 1
            new_rank = player["rank_snapshot"] + 1
            player["current_rank"] = new_rank
            player["rank_snapshot"] = new_rank
            player["status"] = "promoted"
            player["protection_until"] = now + self.config["protection"]
            return {
                "status": "promoted",
                "rank": new_rank,
                "pass_used": True,
                "pass_left": self.inventory["pass"],
                "protection_until": player["protection_until"],
            }
        if player["status"] == "active":
            raise ValueError("series not finished")
        if player["status"] == "ready":
            new_rank = player["rank_snapshot"] + 1
            player["current_rank"] = new_rank
            player["rank_snapshot"] = new_rank
            player["status"] = "promoted"
            player["protection_until"] = now + self.config["protection"]
            return {
                "status": "promoted",
                "rank": new_rank,
                "pass_used": False,
                "pass_left": self.inventory.get("pass", 0),
                "protected": False,
                "protection_until": player["protection_until"],
            }
        protected = now < player.get("protection_until", 0)
        if protected:
            player["status"] = "protected"
            player["current_rank"] = player["rank_snapshot"]
            return {
                "status": "protected",
                "protected": True,
                "rank": player["rank_snapshot"],
            }
        new_rank = player["rank_snapshot"] - 1
        player["current_rank"] = new_rank
        player["rank_snapshot"] = new_rank
        player["status"] = "demoted"
        return {
            "status": "demoted",
            "protected": False,
            "rank": new_rank,
        }

    def settle(self, player_id, season, now=0):
        player = self._player(player_id)
        if player.get("season") != season and player["status"] == "active":
            player["status"] = "expired"
            player["wins"] = 0
            player["losses"] = 0
            player["matches"] = {}
            player["opponents"] = []
            player["season"] = season
            return {
                "status": "expired",
                "season": season,
                "wins": 0,
                "losses": 0,
            }
        return {
            "status": player["status"],
            "season": player["season"],
            "wins": player["wins"],
            "losses": player["losses"],
        }

    def abort(self, player_id, now=0):
        player = self._active(player_id)
        player["status"] = "abandoned"
        player["cooldown_until"] = now + self.config["abandon_cooldown"]
        return {
            "status": "abandoned",
            "cooldown_until": player["cooldown_until"],
        }

    def snapshot(self):
        return copy.deepcopy(self.state)

    def set_rank(self, player_id, rank):
        player = self._player(player_id)
        player["current_rank"] = rank

    def migrate(self, old_id, new_id):
        if old_id not in self.state:
            raise ValueError("player not found")
        self.state[new_id] = self.state.pop(old_id)
