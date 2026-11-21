class AllocationGuard:
    def __init__(self, realm="turn.example.com", nonce_ttl=300, max_lifetime=3600,
                 permission_ttl=300, relay_port_start=49152):
        self.realm = realm
        self.nonce_ttl = nonce_ttl
        self.max_lifetime = max_lifetime
        self.permission_ttl = permission_ttl
        self.relay_port_start = relay_port_start
        self.nonces = {}
        self.allocations = {}
        self.by_tuple = {}
        self.next_port = relay_port_start

    def issue_nonce(self, now):
        raise NotImplementedError

    def allocate(self, request, now):
        raise NotImplementedError

    def create_permission(self, allocation_id, peer_ip, now):
        raise NotImplementedError

    def channel_bind(self, allocation_id, channel, peer_ip, now):
        raise NotImplementedError

    def send(self, allocation_id, peer_ip, now):
        raise NotImplementedError

    def expire(self, now):
        raise NotImplementedError

    def ice_role(self, local_role, remote_role, local_tie, remote_tie):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
