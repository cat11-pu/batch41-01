class HandshakeGuard:
    def __init__(self, psk=None, replay_window=2000, max_handshake_age=120):
        self.psk = psk
        self.replay_window = replay_window
        self.max_handshake_age = max_handshake_age
        self.seen_inits = set()
        self.index_owners = {}
        self.pending = {}
        self.sessions = {}
        self.keys = {}
        self.allowed = {}
        self.keepalives = {}

    def initiate(self, index, peer_pub, timestamp, now):
        raise NotImplementedError

    def cookie_reply(self, client_ip, mac1, valid=True):
        raise NotImplementedError

    def respond(self, index, peer_pub, timestamp, now):
        raise NotImplementedError

    def transport(self, receiver_index, counter, now):
        raise NotImplementedError

    def allow_ip(self, peer, cidr):
        raise NotImplementedError

    def select_allowed_ip(self, peer, ip):
        raise NotImplementedError

    def rotate_key(self, peer, now, grace=60):
        raise NotImplementedError

    def decrypt(self, peer, key_id, now):
        raise NotImplementedError

    def keepalive(self, peer, now):
        raise NotImplementedError

    def validate_message(self, kind, length):
        raise NotImplementedError

    def flush(self, path):
        raise NotImplementedError

    @classmethod
    def load(cls, path):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
