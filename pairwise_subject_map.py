class PairwiseSubjectMap:
    def __init__(self, sector_ttl=3600, salt_grace=60):
        self.sector_ttl = sector_ttl
        self.salt_grace = salt_grace
        self.sectors = {}
        self.salts = {}
        self.mappings = {}
        self.events = []

    def register_sector(self, sector_id, document, now):
        raise NotImplementedError

    def subject_for(self, tenant_id, sector_id, local_subject, email=None,
                    public_sub=None, now=0):
        raise NotImplementedError

    def rotate_salt(self, tenant_id, sector_id, now):
        raise NotImplementedError

    def lookup(self, tenant_id, sector_id, local_subject, now=0):
        raise NotImplementedError

    def sector_status(self, sector_id, now):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
