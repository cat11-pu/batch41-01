import math


class SeasonScoreNormalizer:
    """Normalizes several game modes onto one season ranking scale."""

    DEFAULT_WEIGHTS = {"arena": 0.5, "battlefield": 0.3, "race": 0.2}
    DEFAULT_SCALES = {"arena": 1.0, "battlefield": 100.0, "race": 10.0}
    DEFAULT_CAPS = {"arena": 1000.0, "battlefield": 5000.0, "race": 500.0}

    def __init__(self, weights=None, scales=None, caps=None, min_matches=3,
                 season=1, season_start=0.0, season_end=1000.0,
                 total_cap=1000.0):
        self.weights = dict(weights or self.DEFAULT_WEIGHTS)
        self.scales = dict(scales or self.DEFAULT_SCALES)
        self.caps = dict(caps or self.DEFAULT_CAPS)
        self.min_matches = min_matches
        self.season = season
        self.season_start = season_start
        self.season_end = season_end
        self.total_cap = total_cap
        self._matches = {}
        self._display_names = {}
        self._banned = set()
        self._decay = {mode: 1.0 for mode in self.weights}
        self._archive = []

    def add_match(self, match_id, player_id, mode, score, started_at,
                  ended_at=None, server="s1", season=None,
                  display_name=None, banned=False):
        result = {
            "match_id": match_id,
            "player_id": player_id,
            "counted": False,
            "duplicate": False,
            "reason": None,
        }
        if match_id in self._matches:
            result["duplicate"] = True
            result["reason"] = "duplicate"
            return result
        if started_at < self.season_start:
            result["reason"] = "previous_season"
        elif started_at >= self.season_end:
            result["reason"] = "future_season"
        if result["reason"] is not None:
            return result
        self._matches[match_id] = {
            "player_id": player_id,
            "mode": mode,
            "score": score,
            "started_at": started_at,
            "server": server,
        }
        if display_name is not None:
            self._display_names[player_id] = display_name
        if banned:
            self._banned.add(player_id)
        result["counted"] = True
        return result

    def normalize(self, mode, raw):
        return raw / self.scales[mode]

    def _mode_score(self, mode, raw_total):
        normalized = self.normalize(mode, raw_total)
        capped = min(normalized, self.caps[mode])
        return capped * self.weights[mode] * self._decay[mode]

    def aggregate(self):
        raw_totals = {}
        match_counts = {}
        for match in self._matches.values():
            player_id = match["player_id"]
            mode = match["mode"]
            raw_totals.setdefault(player_id, {})
            raw_totals[player_id][mode] = (
                raw_totals[player_id].get(mode, 0.0) + match["score"]
            )
            match_counts[player_id] = match_counts.get(player_id, 0) + 1
        result = {}
        for player_id, per_mode in raw_totals.items():
            modes = {
                mode: self._mode_score(mode, per_mode.get(mode, 0.0))
                for mode in self.weights
            }
            total = min(sum(modes.values()), self.total_cap)
            result[player_id] = {
                "player_id": player_id,
                "modes": modes,
                "total": total,
                "matches": match_counts[player_id],
            }
        return result

    def _eligible(self, entry):
        return (
            entry["matches"] >= self.min_matches
            and entry["player_id"] not in self._banned
        )

    def rank(self):
        entries = [
            entry for entry in self.aggregate().values() if self._eligible(entry)
        ]
        entries.sort(key=lambda entry: (-entry["total"], entry["player_id"]))
        return entries

    def cutoff(self, percentile):
        ranked = self.rank()
        if not ranked:
            return 0.0
        index = max(math.ceil(percentile * len(ranked)) - 1, 0)
        return ranked[index]["total"]

    def recompute(self, incremental=True):
        return {"players": self.aggregate(), "incremental": incremental}

    def snapshot(self):
        return {
            "season": self.season,
            "aggregate": self.aggregate(),
            "archive": list(self._archive),
        }

    def reset_season(self, new_season, now=0.0):
        self._archive.append({
            "season": self.season,
            "players": self.aggregate(),
            "archived_at": now,
        })
        self._matches = {}
        self._decay = {mode: 1.0 for mode in self.weights}
        window = self.season_end - self.season_start
        self.season = new_season
        self.season_start = now
        self.season_end = now + window

    def apply_decay(self, factor):
        for mode in self._decay:
            self._decay[mode] *= factor

    def set_banned(self, player_id, banned=True):
        if banned:
            self._banned.add(player_id)
        else:
            self._banned.discard(player_id)

    def set_display_name(self, player_id, name):
        self._display_names[player_id] = name
