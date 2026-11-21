class SessionFsm:
    def __init__(self):
        self.sessions = {}

    def open(self, session_id, config, now):
        raise NotImplementedError

    def recv(self, session_id, packet, now, interface=None):
        raise NotImplementedError

    def tick(self, session_id, now):
        raise NotImplementedError

    def admin_down(self, session_id, now):
        raise NotImplementedError

    def admin_up(self, session_id, now):
        raise NotImplementedError

    def rotate_auth(self, session_id, key_id, now, overlap=30):
        raise NotImplementedError

    def state(self, session_id):
        raise NotImplementedError

    def damped(self, session_id, now, window=10):
        raise NotImplementedError

    def flush(self, path):
        raise NotImplementedError

    @classmethod
    def load(cls, path):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
