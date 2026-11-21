"""Server merge cutover ledger."""


class MergeCutover:
    def __init__(self, source=None):
        self.source = dict(source or {})

    def freeze(self, migration_id, characters=None, now=0):
        raise NotImplementedError

    def copy(self, migration_id, batch_id, character_ids, offset=0, limit=None):
        raise NotImplementedError

    def replay(self, migration_id, events):
        raise NotImplementedError

    def commit(self, migration_id):
        raise NotImplementedError

    def rollback(self, migration_id):
        raise NotImplementedError

    def verify(self, migration_id):
        raise NotImplementedError

    def write_source(self, character_id, op="add_gold", amount=0):
        raise NotImplementedError

    def write_target(self, character_id, op="add_gold", amount=0):
        raise NotImplementedError

    def target_state(self, character_id):
        raise NotImplementedError
