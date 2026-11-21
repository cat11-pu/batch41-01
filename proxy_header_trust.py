class ProxyHeaderError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ProxyHeaderTrust:
    def __init__(self, trusted=("10.0.0.0/8", "127.0.0.1/32")):
        self.trusted = list(trusted)

    def parse(self, peer, data):
        raise NotImplementedError

    def accept(self, conn_id, peer, data):
        raise NotImplementedError

    def source(self, conn_id):
        raise NotImplementedError

    def reset(self, conn_id):
        raise NotImplementedError
