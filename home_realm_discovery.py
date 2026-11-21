class RealmDiscoveryIndex:
    def __init__(self, cache_ttl=600):
        self.cache_ttl = cache_ttl
        self.entries = []
        self.version = 0
        self.cache = {}

    def normalize_domain(self, domain):
        raise NotImplementedError

    def add_domain(self, tenant_id, domain, idp_id, verified=False, wildcard=False,
                   priority=0, active=True):
        raise NotImplementedError

    def remove_tenant(self, tenant_id):
        raise NotImplementedError

    def resolve(self, email, now=0):
        raise NotImplementedError

    def cache_info(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
