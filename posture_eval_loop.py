class PostureLoop:
    def __init__(self, ttl=300, offline_grace=3600, max_patch_days=5,
                 token_risk_drop=40, policies=None):
        self.ttl = ttl
        self.offline_grace = offline_grace
        self.max_patch_days = max_patch_days
        self.token_risk_drop = token_risk_drop
        self.policies = list(policies or [])
        self.policy_version = 1
        self.devices = {}
        self.user_devices = {}
        self.cache = {}
        self.tokens = {}
        self.events = []
        self.evaluations = 0

    def report(self, device_id, user_id, signals, now, snapshot=None):
        raise NotImplementedError

    def evaluate(self, device_id, now, timed_out=False):
        raise NotImplementedError

    def aggregate(self, user_id, now):
        raise NotImplementedError

    def issue_token(self, user_id, now):
        raise NotImplementedError

    def token_status(self, token):
        raise NotImplementedError

    def upgrade_policy(self, policies, now):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
