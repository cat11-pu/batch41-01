class DnsScorerError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TunnelScorer:
    def __init__(self, whitelist=(), threshold=1.5, window=60.0):
        self.whitelist = list(whitelist)
        self.threshold = threshold
        self.window = window

    def normalize(self, name):
        raise NotImplementedError

    def is_whitelisted(self, name):
        raise NotImplementedError

    def observe(self, event):
        raise NotImplementedError

    def ingest(self, events):
        raise NotImplementedError

    def features(self, client, now=None):
        raise NotImplementedError

    def score(self, client, now=None):
        raise NotImplementedError

    def verdict(self, client, now=None):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def merge_state(self, state):
        raise NotImplementedError
