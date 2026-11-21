class UsmGuard:
    """SNMPv3 USM engine discovery, authentication and view control."""

    def __init__(self, engine_id='8000000001020304', users=None, max_bulk=100, trusted_engines=None):
        raise NotImplementedError()

    def discover(self, response):
        raise NotImplementedError()

    def _auth_failure(self):
        raise NotImplementedError()

    def authenticate(self, message):
        raise NotImplementedError()

    def check_timeliness(self, message, now):
        raise NotImplementedError()

    def advance_boot(self):
        raise NotImplementedError()

    def _view_allows(self, user, oid, write):
        raise NotImplementedError()

    def authorize_view(self, user, oid, write=False):
        raise NotImplementedError()

    def get_bulk(self, user, oid, repetitions):
        raise NotImplementedError()

    def match_response(self, request_id, response):
        raise NotImplementedError()

    def handle_report(self, report):
        raise NotImplementedError()

    def build_notification(self, kind):
        raise NotImplementedError()

    def export_state(self):
        raise NotImplementedError()

    def restore_state(self, state):
        raise NotImplementedError()

    def snapshot(self):
        raise NotImplementedError()
