"""Activity milestone tracking with separate personal and global ledgers."""

import json


DEFAULT_CONFIG = {
    "period": "p1",
    "start": 0,
    "end": 1000,
    "personal_cap": 100,
    "global_cap": 100000,
    "tiers": [10, 30, 60],
    "global_tiers": [50, 100],
    "version": 1,
}


class MilestoneTrack:
    def __init__(self, config=None, server_map=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.config["tiers"] = list(self.config.get("tiers", []))
        self.config["global_tiers"] = list(self.config.get("global_tiers", []))
        self.server_map = dict(server_map or {})

        self._personal = {}
        self._global = 0
        self._claims = {}
        self._claim_keys = {}
        self._task_awards = set()
        self._compensations = {}
        self._archives = {}
        self._config_history = {
            self.config["version"]: self._copy_config(self.config)
        }

    @staticmethod
    def _copy_config(config):
        copied = dict(config)
        copied["tiers"] = list(config.get("tiers", []))
        copied["global_tiers"] = list(config.get("global_tiers", []))
        return copied

    def _resolve_player(self, player_id, server_id=None):
        if server_id is None:
            return player_id
        return self.server_map.get((server_id, player_id), player_id)

    def _claimed_tiers(self, player_id):
        return self._claims.setdefault(player_id, set())

    def award(self, player_id, task_id, points, now, server_id=None):
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points < 0:
            raise ValueError("points must be a non-negative number")

        start = self.config.get("start")
        end = self.config.get("end")
        if (start is not None and now < start) or (end is not None and now >= end):
            raise ValueError("award is outside the activity window")

        player_id = self._resolve_player(player_id, server_id)
        task_key = (self.config["period"], player_id, task_id)
        if task_key in self._task_awards:
            return {
                "applied": 0,
                "duplicate": True,
                "personal": self._personal.get(player_id, 0),
                "global": self._global,
                "player_id": player_id,
                "task_id": task_id,
            }

        personal = self._personal.get(player_id, 0)
        personal_room = max(self.config["personal_cap"] - personal, 0)
        global_room = max(self.config["global_cap"] - self._global, 0)
        applied = min(points, personal_room, global_room)

        if applied:
            personal += applied
            self._personal[player_id] = personal
            self._global += applied

        # A task is consumed even if a cap leaves no room for its points.
        self._task_awards.add(task_key)
        return {
            "applied": applied,
            "duplicate": False,
            "personal": personal,
            "global": self._global,
            "player_id": player_id,
            "task_id": task_id,
        }

    def claim(self, player_id, tier, idempotency_key=None):
        player_id = self._resolve_player(player_id)
        tiers = self.config["tiers"]
        if not isinstance(tier, int) or isinstance(tier, bool) or tier < 1 or tier > len(tiers):
            raise ValueError("unknown milestone tier")

        claimed = self._claimed_tiers(player_id)
        keys = self._claim_keys.setdefault(player_id, {})
        if idempotency_key is not None and idempotency_key in keys:
            if keys[idempotency_key] != tier:
                raise ValueError("idempotency key was already used for another tier")
            return {
                "tier": tier,
                "duplicate": True,
                "claimed": sorted(claimed),
            }

        if tier in claimed:
            if idempotency_key is not None:
                keys[idempotency_key] = tier
            return {
                "tier": tier,
                "duplicate": True,
                "claimed": sorted(claimed),
            }

        for earlier in range(1, tier):
            if earlier not in claimed:
                raise ValueError("milestone tiers must be claimed in order")

        if self._personal.get(player_id, 0) < tiers[tier - 1]:
            raise ValueError("milestone threshold has not been reached")

        claimed.add(tier)
        if idempotency_key is not None:
            keys[idempotency_key] = tier
        return {
            "tier": tier,
            "duplicate": False,
            "claimed": sorted(claimed),
        }

    def progress(self, player_id):
        player_id = self._resolve_player(player_id)
        personal = self._personal.get(player_id, 0)
        claimed = sorted(self._claims.get(player_id, set()))
        return {
            "period": self.config["period"],
            "personal": personal,
            "global": self._global,
            "ranking_points": personal,
            "claimed": claimed,
            "next_tier": self._next_claimable_tier(personal, claimed),
            "version": self.config["version"],
        }

    def _next_claimable_tier(self, personal, claimed):
        for tier, threshold in enumerate(self.config["tiers"], start=1):
            if tier in claimed:
                continue
            return tier if personal >= threshold else 0
        return 0

    def compensate(self, player_id, snapshot):
        if not isinstance(snapshot, dict) or "points" not in snapshot:
            raise ValueError("a compensation snapshot must contain points")
        snapshot_points = snapshot["points"]
        if (
            not isinstance(snapshot_points, (int, float))
            or isinstance(snapshot_points, bool)
            or snapshot_points < 0
        ):
            raise ValueError("snapshot points must be a non-negative number")

        player_id = self._resolve_player(player_id)
        snapshot_key = json.dumps(snapshot, sort_keys=True, default=str)
        seen = self._compensations.setdefault(player_id, set())
        if snapshot_key in seen:
            return {
                "granted": [],
                "duplicate": True,
                "claimed": sorted(self._claims.get(player_id, set())),
            }

        version = snapshot.get("version")
        historical = self._config_history.get(version, self.config)
        tiers = snapshot.get("tiers", historical.get("tiers", []))

        claimed = self._claimed_tiers(player_id)
        granted = []
        for tier, threshold in enumerate(tiers, start=1):
            if tier in claimed:
                continue
            if snapshot_points < threshold:
                break
            claimed.add(tier)
            granted.append(tier)

        seen.add(snapshot_key)
        return {
            "granted": granted,
            "duplicate": False,
            "claimed": sorted(claimed),
        }

    def apply_config(self, config):
        version = config.get("version")
        if version is None:
            raise ValueError("config version is required")
        if version < self.config["version"]:
            raise ValueError("stale config version")

        self._config_history[self.config["version"]] = self._copy_config(self.config)
        updated = dict(self.config)
        updated.update(config)
        updated["tiers"] = list(updated.get("tiers", []))
        updated["global_tiers"] = list(updated.get("global_tiers", []))
        self.config = updated
        self._config_history[version] = self._copy_config(updated)
        return {"version": version}

    def rotate(self, period):
        archived_period = self.config["period"]
        self._archives[archived_period] = {
            "personal": dict(self._personal),
            "global": self._global,
            "claims": {
                player_id: sorted(tiers)
                for player_id, tiers in self._claims.items()
            },
        }

        self._personal.clear()
        self._global = 0
        self._claims.clear()
        self._claim_keys.clear()
        self._task_awards.clear()
        self._compensations.clear()
        self.config["period"] = period
        return {"archived_period": archived_period, "period": period}
