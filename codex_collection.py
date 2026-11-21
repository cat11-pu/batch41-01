"""Account shared codex collection with sets and backfill."""


class CodexCollection:
    def __init__(self, entries=None, sets=None, season="S1", state=None):
        self.entries = dict(entries or {})
        self.sets = dict(sets or {})
        self.season = season
        self.state = state

    def unlock(self, account_id, entry_id, character_id=None, season=None, achieved=False):
        raise NotImplementedError

    def merge(self, account_id, snapshot):
        raise NotImplementedError

    def grant(self, account_id, set_id):
        raise NotImplementedError

    def backfill(self, accounts, batch_size=100):
        raise NotImplementedError

    def revoke(self, account_id, entry_id):
        raise NotImplementedError

    def detail(self, account_id):
        raise NotImplementedError

    def leaderboard(self):
        raise NotImplementedError
