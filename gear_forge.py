"""Gear enhancement forge."""

DEFAULT_CONFIG = {
    "success_rate": 0.4,
    "pity_limit": 3,
    "cost": {"ore": 1},
    "stat_per_level": {"attack": 10},
}


class GearForge:
    def __init__(self, gears=None, inventory=None, config=None, roll=None, state=None):
        self.gears = dict(gears or {})
        self.inventory = dict(inventory or {})
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.roll = roll
        self.state = state

    def preview(self, gear_id):
        raise NotImplementedError

    def forge(self, gear_id, version, material="ore"):
        raise NotImplementedError

    def protect(self, gear_id):
        raise NotImplementedError

    def transfer(self, source_id, target_id, version):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
