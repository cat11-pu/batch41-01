class VrrpFsm:
    """VRRP master/backup election with preemption and tracking."""

    def __init__(self, vrid, priority, ip='10.0.0.1', advert_interval=1.0, preempt=True, auth_key='vrrp', domain='lan-a', track_penalty=10):
        raise NotImplementedError()

    def current_state(self):
        raise NotImplementedError()

    def effective_priority(self):
        raise NotImplementedError()

    def skew_time(self):
        raise NotImplementedError()

    def start(self, now=0.0):
        raise NotImplementedError()

    def _ignore(self, reason):
        raise NotImplementedError()

    def receive(self, advert, now):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def set_tracking(self, ok):
        raise NotImplementedError()

    def virtual_mac(self):
        raise NotImplementedError()

    def send_advert(self):
        raise NotImplementedError()

    def resolve_split_brain(self, peers):
        raise NotImplementedError()
