"""Activity milestone tracking with separate personal and global ledgers."""

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
        self.server_map = dict(server_map or {})

    def award(self, player_id, task_id, points, now, server_id=None):
        raise NotImplementedError

    def claim(self, player_id, tier, idempotency_key=None):
        raise NotImplementedError

    def progress(self, player_id):
        raise NotImplementedError

    def compensate(self, player_id, snapshot):
        raise NotImplementedError

    def apply_config(self, config):
        raise NotImplementedError

    def rotate(self, period):
        raise NotImplementedError
