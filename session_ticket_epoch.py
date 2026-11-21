class TicketEpochKeeper:
    def __init__(self, state_path=None, grace=300, ticket_ttl=600, early_data_limit=16384):
        self.state_path = state_path
        self.grace = grace
        self.ticket_ttl = ticket_ttl
        self.early_data_limit = early_data_limit
        self.epochs = {}
        self.current_epoch = 1
        self.revoked = set()
        self.identities = {}
        self.replay = set()
        self.events = []

    def bind_identity(self, fingerprint, identity):
        raise NotImplementedError

    def revoke_cert(self, fingerprint):
        raise NotImplementedError

    def rotate(self, now):
        raise NotImplementedError

    def issue(self, fingerprint, alpn, now):
        raise NotImplementedError

    def resume(self, ticket, fingerprint, alpn, method="GET", early_data=b"",
               replay_key=None, now=0, chain_ok=True):
        raise NotImplementedError

    def flush(self):
        raise NotImplementedError

    def ledger(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
