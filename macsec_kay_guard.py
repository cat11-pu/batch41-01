class KayGuard:
    """MACsec MKA key agreement and secure association state."""

    def __init__(self, cak_name='cak-prod', sci='aa:bb:cc:dd:ee:ff:0001', cipher_suite='gcm-aes-128', key_server_priority=0):
        raise NotImplementedError()

    @staticmethod
    def _split_sci(sci):
        raise NotImplementedError()

    def join_ca(self, cak_name, sci):
        raise NotImplementedError()

    def elect_key_server(self, peers):
        raise NotImplementedError()

    def install_sak(self, sak, now=0.0):
        raise NotImplementedError()

    def _next_association(self):
        raise NotImplementedError()

    def _next_key_number(self):
        raise NotImplementedError()

    def rotate(self, now, lifetime=30.0):
        raise NotImplementedError()

    def expire(self, now):
        raise NotImplementedError()

    def sa_state(self, association):
        raise NotImplementedError()

    def _icv(self, sa, pn, payload):
        raise NotImplementedError()

    def encrypt(self, pn, payload, association=0, now=0.0):
        raise NotImplementedError()

    def decrypt(self, packet, now=0.0):
        raise NotImplementedError()

    def receive_mka(self, packet):
        raise NotImplementedError()

    def link_down(self):
        raise NotImplementedError()

    def link_up(self, now=0.0):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
