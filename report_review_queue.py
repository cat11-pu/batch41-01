"""Report review queue with ticket and punishment state linkage."""

DEFAULT_CONFIG = {
    "cooldown": 100,
    "appeal_window": 200,
    "evidence_retention": 300,
    "malicious_penalty": 10,
}


class ReviewQueue:
    def __init__(self, config=None, identities=None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.identities = dict(identities or {})

    def file(self, reporter_id, target_id, match_id, evidence, now=0, server_id=None):
        raise NotImplementedError

    def assign(self, ticket_id, reviewer_id):
        raise NotImplementedError

    def decide(self, ticket_id, reviewer_id, verdict, punishment=None,
               idempotency_key=None, now=0, appeal=False):
        raise NotImplementedError

    def appeal(self, ticket_id, reason, now=0):
        raise NotImplementedError

    def enforce(self, ticket_id, now=0):
        raise NotImplementedError

    def batch_decide(self, decisions):
        raise NotImplementedError
