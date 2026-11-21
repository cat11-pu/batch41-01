class ProxyChainError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class HopState:
    def __init__(self, hops=(), self_token="chain-local", max_auth_retries=2):
        self.hops = list(hops)
        self.self_token = self_token
        self.max_auth_retries = max_auth_retries

    def sanitize_headers(self, headers, hop_id):
        raise NotImplementedError

    def auth_for(self, proxy_id):
        raise NotImplementedError

    def rewrite_target(self, request, target):
        raise NotImplementedError

    def decode_body(self, body, headers):
        raise NotImplementedError

    def tunnel_state(self, events):
        raise NotImplementedError

    def effective_idle_timeout(self):
        raise NotImplementedError

    def connection_key(self, hop_id, target):
        raise NotImplementedError

    def acquire(self, hop_id, target):
        raise NotImplementedError

    def on_407(self, proxy_id):
        raise NotImplementedError

    def match_responses(self, requests, responses):
        raise NotImplementedError
