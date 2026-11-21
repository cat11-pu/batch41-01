class TopologyGuard:
    """RSTP root election, port role and topology change guard."""

    def __init__(self, bridge_id='32768.00:11:22:33:44:55', max_age=20, bpdu_guard=True, root_guard=True, tc_limit=10):
        raise NotImplementedError()

    @staticmethod
    def _bridge_key(bridge_id):
        raise NotImplementedError()

    def add_port(self, name, edge=False, guard=None, root_guard=False):
        raise NotImplementedError()

    def root_id(self):
        raise NotImplementedError()

    def root_port(self):
        raise NotImplementedError()

    def port_state(self, name):
        raise NotImplementedError()

    def port_role(self, name):
        raise NotImplementedError()

    def path_cost(self, speed_mbps):
        raise NotImplementedError()

    def _handle_tc(self, now):
        raise NotImplementedError()

    def tc_events(self):
        raise NotImplementedError()

    def receive_bpdu(self, bpdu, port, now=0.0):
        raise NotImplementedError()

    def process_proposal(self, port, agreement=False):
        raise NotImplementedError()

    def advance_port(self, port):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
