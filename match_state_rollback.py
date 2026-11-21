class MatchStateRollback:
    """Coordinates ranked-match rollback across rank, rewards and progress."""

    RANK_DELTA = {"win": 25, "loss": -20}
    HIDDEN_DELTA = {"win": 25, "loss": -20}

    def __init__(self, season=2, item_values=None):
        self.season = season
        self.item_values = dict(item_values or {})

    def record_match(self, match_id, result, players, season=None,
                     quests_after=None, achievements_after=None,
                     rewards=None, now=0.0):
        raise NotImplementedError

    def plan(self, match_id, operator="system"):
        raise NotImplementedError

    def apply(self, plan_id, operator="system"):
        raise NotImplementedError

    def revert(self, plan_id, operator="system"):
        raise NotImplementedError

    def compensate(self, plan_id, reason="manual", operator="system"):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def query(self, player_id):
        raise NotImplementedError

    def claim_reward(self, match_id, player_id, item):
        raise NotImplementedError

    def login(self, player_id):
        raise NotImplementedError

    def maintenance(self, match_id):
        raise NotImplementedError
