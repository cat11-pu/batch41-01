class ZoneTransferGuard:
    """AXFR/IXFR transaction, journal cursor and TSIG validation."""

    def __init__(self, zone='example.internal', allowed_sources=None, max_records=1000, tsig_key='tsig-secret', tsig_fudge=300, journal_retention=3):
        raise NotImplementedError()

    @staticmethod
    def serial_gt(left, right):
        raise NotImplementedError()

    def load_zone(self, records, serial=0):
        raise NotImplementedError()

    def current_serial(self):
        raise NotImplementedError()

    def zone_records(self):
        raise NotImplementedError()

    def _signature(self, serial, seq):
        raise NotImplementedError()

    def _journal_covers(self, client_serial):
        raise NotImplementedError()

    def handle_request(self, request):
        raise NotImplementedError()

    def apply_transfer(self, messages, from_addr=None, now=0.0):
        raise NotImplementedError()

    def notify(self, serial, source=None):
        raise NotImplementedError()

    def refresh(self, slave_serial):
        raise NotImplementedError()

    def advance_cursor(self, slave, serial):
        raise NotImplementedError()

    def compression_name(self, data, offset=0):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
