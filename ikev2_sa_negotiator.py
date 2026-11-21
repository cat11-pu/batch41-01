class IkeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SaNegotiator:
    def __init__(self, supported=None, require_cookie=False):
        if supported is None:
            supported = [
                {"encr": "AES_256_CBC", "integ": "HMAC_SHA2_256",
                 "dh": "MODP_2048", "key_length": 256},
                {"encr": "AES_128_CBC", "integ": "HMAC_SHA2_256",
                 "dh": "MODP_2048", "key_length": 128},
            ]
        self.supported = list(supported)
        self.require_cookie = require_cookie

    def initiate(self, exchange):
        raise NotImplementedError

    def handle_init(self, request):
        raise NotImplementedError

    def select_proposal(self, offered):
        raise NotImplementedError

    def add_tunnel(self, label, local_ts, remote_ts):
        raise NotImplementedError

    def add_child(self, ike_id, label, local_ts, remote_ts):
        raise NotImplementedError

    def child(self, ike_id, child_id):
        raise NotImplementedError

    def delete_child(self, ike_id, child_id):
        raise NotImplementedError

    def resolve_rekey(self, local, remote):
        raise NotImplementedError

    def narrow_selectors(self, local, remote):
        raise NotImplementedError

    def dpd(self, ike_id, now):
        raise NotImplementedError

    def parse_nat_t(self, packet):
        raise NotImplementedError

    def replay_check(self, sa_id, seq):
        raise NotImplementedError

    def rekey(self, sa_id):
        raise NotImplementedError

    def reassemble(self, fragments):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def import_state(self, state):
        raise NotImplementedError

    def sa_count(self):
        raise NotImplementedError
