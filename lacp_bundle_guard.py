class BundleGuard:
    def __init__(self, group_id="bundle-1", system_id="00:11:22:33:44:55",
                 system_priority=32768, mode="active", timeout="short",
                 min_links=1, cooldown=5, required_speed=None):
        self.group_id = group_id
        self.system_id = system_id
        self.system_priority = system_priority
        self.mode = mode
        self.timeout = timeout
        self.min_links = min_links
        self.cooldown = cooldown
        self.required_speed = required_speed
        self.members = {}
        self.mac_table_db = {}

    def add_member(self, port, speed=None, mode=None):
        raise NotImplementedError

    def recv_lacpdu(self, port, lacpdu, now):
        raise NotImplementedError

    def member_state(self, port, now):
        raise NotImplementedError

    def select(self, now):
        raise NotImplementedError

    def aggregate_status(self, now):
        raise NotImplementedError

    def flap(self, port, now):
        raise NotImplementedError

    def restart(self, now):
        raise NotImplementedError

    def learn_mac(self, mac, port, now=0):
        raise NotImplementedError

    def mac_table(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
