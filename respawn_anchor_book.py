class RespawnAnchor:
    """Per-instance respawn anchors, boss progress and loot snapshots."""

    def __init__(self):
        pass

    def create_instance(self, instance_id, dungeon_id, team_id,
                        difficulty="normal", entrance=(0, 0),
                        walkable=None, anchors=None, bosses=None):
        raise NotImplementedError

    def activate(self, instance_id, anchor_id, actor=None):
        raise NotImplementedError

    def current_anchor(self, instance_id):
        raise NotImplementedError

    def bind(self, instance_id, player_id, now=0.0):
        raise NotImplementedError

    def respawn(self, instance_id, player_id, now=0.0):
        raise NotImplementedError

    def kill_boss(self, instance_id, boss_id, roster, anchor_id=None, now=0.0):
        raise NotImplementedError

    def reset_boss(self, instance_id, boss_id, now=0.0):
        raise NotImplementedError

    def state(self, instance_id, entity_id):
        raise NotImplementedError

    def reset_instance(self, instance_id, now=0.0):
        raise NotImplementedError

    def restore(self, instance_id, player_id):
        raise NotImplementedError

    def join_late(self, instance_id, player_id):
        raise NotImplementedError

    def wipe(self, instance_id, now=0.0):
        raise NotImplementedError

    def start_wipe_timer(self, instance_id, seconds, now=0.0):
        raise NotImplementedError

    def loot_eligible(self, instance_id, boss_id, player_id):
        raise NotImplementedError

    def change_leader(self, instance_id, new_leader):
        raise NotImplementedError
