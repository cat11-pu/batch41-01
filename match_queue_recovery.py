class MatchQueueRecovery:
    """Durable matchmaking queue with snapshots, journal and leases."""

    def __init__(self, path, snapshot_every=60.0):
        self.path = path
        self.snapshot_every = snapshot_every

    def enqueue(self, ticket_id, players, priority=0, group=None,
                expires_at=None, now=0.0):
        raise NotImplementedError

    def persist(self, now=0.0):
        raise NotImplementedError

    def restore(self, now=0.0):
        raise NotImplementedError

    def cancel(self, ticket_id, now=0.0):
        raise NotImplementedError

    def claim(self, ticket_id, node_id, now=0.0, lease=30.0):
        raise NotImplementedError

    def match(self, ticket_ids, match_id, node_id, now=0.0, fail_create=False):
        raise NotImplementedError

    def compensate(self, match_id, now=0.0):
        raise NotImplementedError

    def reconnect(self, player_id, ticket_id=None, now=0.0):
        raise NotImplementedError

    def update(self, ticket_id, changes, expected_version, now=0.0):
        raise NotImplementedError

    def ticket(self, ticket_id):
        raise NotImplementedError

    def tickets(self):
        raise NotImplementedError

    def matches(self):
        raise NotImplementedError

    def peek(self):
        raise NotImplementedError
