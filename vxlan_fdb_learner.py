class FdbLearner:
    def __init__(self, age_seconds=300, arp_ttl=60):
        self.age_seconds = age_seconds
        self.arp_ttl = arp_ttl
        self.fdb = {}
        self.vteps = {}
        self.arp = {}
        self.lock = None

    def join_vtep(self, vni, vtep):
        raise NotImplementedError

    def learn(self, vni, mac, vtep, seq, now, static=False):
        raise NotImplementedError

    def lookup(self, vni, mac, now):
        raise NotImplementedError

    def age(self, now):
        raise NotImplementedError

    def bum_replicators(self, vni, source_vtep):
        raise NotImplementedError

    def learn_arp(self, vni, ip, mac, now):
        raise NotImplementedError

    def resolve_arp(self, vni, ip, now):
        raise NotImplementedError

    def validate_encap(self, packet):
        raise NotImplementedError

    def mtu_for(self, underlay_mtu=None):
        raise NotImplementedError

    def detect_loop(self, vtep_path):
        raise NotImplementedError

    def parse_evpn_type2(self, payload):
        raise NotImplementedError

    def sync_controller(self, vni, routes):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
