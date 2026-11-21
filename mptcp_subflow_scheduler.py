class MptcpError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SubflowScheduler:
    def __init__(self, mode="lowest-rtt"):
        self.mode = mode

    def add_subflow(self, subflow):
        raise NotImplementedError

    def remove_subflow(self, subflow_id):
        raise NotImplementedError

    def remove_address(self, addr_id):
        raise NotImplementedError

    def select(self):
        raise NotImplementedError

    def plan(self, total):
        raise NotImplementedError

    def map_segment(self, dsn, length, subflow_id, ssn):
        raise NotImplementedError

    def track(self, segment):
        raise NotImplementedError

    def mark_failed(self, subflow_id):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def handshake(self, result):
        raise NotImplementedError

    def batch_assign(self, count):
        raise NotImplementedError

    def dedupe(self, segments):
        raise NotImplementedError

    def resolve_open(self, local_token, remote_token):
        raise NotImplementedError

    def set_priority(self, subflow_id, priority):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def import_state(self, state):
        raise NotImplementedError

    def subflow(self, subflow_id):
        raise NotImplementedError

    def subflows(self):
        raise NotImplementedError

    def probe_targets(self):
        raise NotImplementedError
