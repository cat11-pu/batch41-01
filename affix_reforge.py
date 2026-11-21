"""Affix reforge with locked affixes, preview tokens and history."""


class AffixReforge:
    def __init__(self, pool=None, items=None, inventory=None, cost=None,
                 roll=None, state=None):
        self.pool = list(pool or [])
        self.items = dict(items or {})
        self.inventory = dict(inventory or {})
        self.cost = dict(cost or {})
        self.roll = roll
        self.state = state

    def preview(self, item_id):
        raise NotImplementedError

    def reforge(self, item_id, token=None):
        raise NotImplementedError

    def lock(self, item_id, affix_ids):
        raise NotImplementedError

    def undo(self, item_id):
        raise NotImplementedError

    def history(self, item_id):
        raise NotImplementedError
