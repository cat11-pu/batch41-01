class EapolAuthenticator:
    """802.1X port authentication with MAB fallback and guest VLAN."""

    def __init__(self, radius_secret='radius-secret', reauth_interval=3600, mab_enabled=True, dot1x_enabled=True, guest_vlan=99, auth_vlan=10, mab_db=None):
        raise NotImplementedError()

    @staticmethod
    def normalize_mac(mac):
        raise NotImplementedError()

    def add_port(self, name):
        raise NotImplementedError()

    def port_state(self, name):
        raise NotImplementedError()

    def port_vlan(self, name):
        raise NotImplementedError()

    def is_authorized(self, port, mac):
        raise NotImplementedError()

    def on_eapol(self, frame, now=0.0):
        raise NotImplementedError()

    def _radius_digest(self, code):
        raise NotImplementedError()

    def on_radius(self, response):
        raise NotImplementedError()

    def try_mab(self, port, mac, now=0.0):
        raise NotImplementedError()

    def tick(self, now):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
