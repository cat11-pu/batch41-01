class BestPathArbiter:
    """BGP best path selection with loop, policy and withdrawal handling."""

    def __init__(self, local_as=65000, router_id='192.0.2.1', max_prefixes=1000):
        raise NotImplementedError()

    def add_peer(self, peer, remote_as, address=None):
        raise NotImplementedError()

    def peer_state(self, peer):
        raise NotImplementedError()

    def path_length(self, as_path, as_set=None, confed_path=None):
        raise NotImplementedError()

    def _shut_peer(self, peer):
        raise NotImplementedError()

    def update(self, message):
        raise NotImplementedError()

    def withdraw(self, prefix, peer=None):
        raise NotImplementedError()

    def select(self, candidates):
        raise NotImplementedError()

    def best_path(self, prefix):
        raise NotImplementedError()

    def fib(self):
        raise NotImplementedError()

    def route_refresh(self, peer):
        raise NotImplementedError()

    def finish_refresh(self, peer, kept_prefixes):
        raise NotImplementedError()

    def mark_stale(self, peer, deadline):
        raise NotImplementedError()

    def expire_stale(self, now):
        raise NotImplementedError()

    def multipath(self, candidates):
        raise NotImplementedError()

    def apply(self, events):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
