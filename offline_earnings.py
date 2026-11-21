"""Offline earnings settlement with mail and claim records."""

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
        self.state = state

    def mark_offline(self, player_id, at, team=None, guild=None,
                     guild_bonus=0.0, double=False):
        raise NotImplementedError

    def settle(self, player_id, now):
        raise NotImplementedError

    def claim(self, player_id, now=0, bag_space=999, claim_key=None):
        raise NotImplementedError

    def expire(self, player_id, now):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def update_online_state(self, player_id, team=None, guild=None):
        raise NotImplementedError
