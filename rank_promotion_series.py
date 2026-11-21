"""Ranked promotion series with best-of-three state."""

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
        self.state = state

    def start(self, player_id, season, rank, now=0):
        raise NotImplementedError

    def record(self, player_id, match_id, result, opponent_id=None,
               disconnected=False, now=0):
        raise NotImplementedError

    def advance(self, player_id, now=0, use_pass=False):
        raise NotImplementedError

    def settle(self, player_id, season, now=0):
        raise NotImplementedError

    def abort(self, player_id, now=0):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def set_rank(self, player_id, rank):
        raise NotImplementedError

    def migrate(self, old_id, new_id):
        raise NotImplementedError
