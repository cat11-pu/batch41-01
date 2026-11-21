class SynCookieError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SynCookieGuard:
    def __init__(self, secret=b"cookie-secret", queue_limit=2, timeout=30.0,
                 rotate_interval=60.0, overlap=30.0, quiet_period=5.0,
                 sync_threshold=4, attack_window=5.0, max_retransmit=16.0,
                 whitelist=(), trusted_proxies=()):
        self.secret = secret
        self.queue_limit = queue_limit
        self.timeout = timeout
        self.rotate_interval = rotate_interval
        self.overlap = overlap
        self.quiet_period = quiet_period
        self.sync_threshold = sync_threshold
        self.attack_window = attack_window
        self.max_retransmit = max_retransmit
        self.whitelist = list(whitelist)
        self.trusted_proxies = list(trusted_proxies)

    def receive_syn(self, packet, now=0.0):
        raise NotImplementedError

    def receive_ack(self, packet, now=0.0):
        raise NotImplementedError

    def expire(self, now):
        raise NotImplementedError

    def rotate_key(self, now):
        raise NotImplementedError

    def retransmit_interval(self, attempt):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def under_attack(self, now):
        raise NotImplementedError

    def mode(self, now):
        raise NotImplementedError

    def source_of(self, packet):
        raise NotImplementedError

    def table_sizes(self):
        raise NotImplementedError
