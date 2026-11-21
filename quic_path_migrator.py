class PathMigrator:
    """Server-side QUIC path migration state machine."""

    def __init__(self, migration_enabled=True, validation_timeout=5.0, amplification_factor=3):
        raise NotImplementedError()

    def add_path(self, path_id, addr, cwnd=10.0, rtt=0.05, worker='worker-1'):
        raise NotImplementedError()

    def activate(self, path_id):
        raise NotImplementedError()

    def active_path(self):
        raise NotImplementedError()

    def path_addr(self, path_id):
        raise NotImplementedError()

    def path_state(self, path_id):
        raise NotImplementedError()

    def _challenge(self, path_id, now):
        raise NotImplementedError()

    def on_packet(self, packet):
        raise NotImplementedError()

    def validate(self, path_id, token):
        raise NotImplementedError()

    def commit(self, path_id):
        raise NotImplementedError()

    def check_timeout(self, now):
        raise NotImplementedError()

    def send_budget(self, path_id):
        raise NotImplementedError()

    def record_received(self, path_id, nbytes):
        raise NotImplementedError()

    def send(self, path_id, nbytes):
        raise NotImplementedError()

    def congestion(self):
        raise NotImplementedError()

    def register_cid(self, dcid, path_id):
        raise NotImplementedError()

    def route(self, dcid):
        raise NotImplementedError()

    def accept_pn(self, path_id, pn):
        raise NotImplementedError()

    def offer_preferred(self, path_id, addr, cwnd=20.0, rtt=0.03, worker='worker-1'):
        raise NotImplementedError()

    def export_state(self):
        raise NotImplementedError()

    def load_state(self, state):
        raise NotImplementedError()
