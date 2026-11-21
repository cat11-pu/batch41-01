class SipError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RegisterGuard:
    def __init__(self, domains=(), min_refresh=60, bulk_limit=5, default_expires=3600, nonce_ttl=300):
        self.domains = set(domains)
        self.min_refresh = min_refresh
        self.bulk_limit = bulk_limit
        self.default_expires = default_expires
        self.nonce_ttl = nonce_ttl

    def issue_nonce(self, aor, now=0.0):
        raise NotImplementedError

    def challenge(self, aor, now=0.0):
        raise NotImplementedError

    def register(self, request, now=0.0):
        raise NotImplementedError

    def register_bulk(self, requests, now=0.0):
        raise NotImplementedError

    def bindings(self, aor, now=None):
        raise NotImplementedError

    def targets(self, aor, now=None):
        raise NotImplementedError

    def primary_target(self, aor, now=None):
        raise NotImplementedError

    def match_response(self, response):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
