class AdjacencyGuard:
    """OSPF adjacency authentication, LSA validation and state machine."""

    def __init__(self, router_id='1.1.1.1', area_id='0.0.0.0', auth_key='s3cret', interfaces=None, max_retransmit=3, allow_external=False):
        raise NotImplementedError()

    def _record(self, accepted, reason, actor=None):
        raise NotImplementedError()

    def _reject(self, reason, actor=None):
        raise NotImplementedError()

    def _accept(self, actor=None, **extra):
        raise NotImplementedError()

    def register_neighbor(self, router_id, state='Down'):
        raise NotImplementedError()

    def on_hello(self, packet):
        raise NotImplementedError()

    def on_dd(self, packet):
        raise NotImplementedError()

    def on_lsa(self, packet):
        raise NotImplementedError()

    def on_lsr(self, packet):
        raise NotImplementedError()

    def transition(self, router_id, new_state):
        raise NotImplementedError()

    def elect_dr(self, candidates):
        raise NotImplementedError()

    def lsdb_digest(self):
        raise NotImplementedError()

    def restore_lsdb(self, lsas, expected_digest=None):
        raise NotImplementedError()

    def lsdb_entry(self, lsa_type, lsa_id, adv_router):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
