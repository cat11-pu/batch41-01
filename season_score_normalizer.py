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

    def add_match(self, match_id, player_id, mode, score, started_at,
                  ended_at=None, server="s1", season=None,
                  display_name=None, banned=False):
        raise NotImplementedError

    def normalize(self, mode, raw):
        raise NotImplementedError

    def aggregate(self):
        raise NotImplementedError

    def rank(self):
        raise NotImplementedError

    def cutoff(self, percentile):
        raise NotImplementedError

    def recompute(self, incremental=True):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def reset_season(self, new_season, now=0.0):
        raise NotImplementedError

    def apply_decay(self, factor):
        raise NotImplementedError

    def set_banned(self, player_id, banned=True):
        raise NotImplementedError

    def set_display_name(self, player_id, name):
        raise NotImplementedError
