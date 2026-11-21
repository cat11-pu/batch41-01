class CreditError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CreditGuard:
    def __init__(self, session_timeout=60, max_retries=2):
        self.session_timeout = session_timeout
        self.max_retries = max_retries

    def credit_control(self, request):
        raise NotImplementedError

    def create_session(self, request):
        raise NotImplementedError

    def update_session(self, request):
        raise NotImplementedError

    def terminate_session(self, request):
        raise NotImplementedError

    def match_response(self, response):
        raise NotImplementedError

    def available(self, subscriber):
        raise NotImplementedError

    def session(self, session_id):
        raise NotImplementedError

    def active_sessions(self):
        raise NotImplementedError

    def cleanup(self, now):
        raise NotImplementedError

    def export_state(self):
        raise NotImplementedError

    def import_state(self, state):
        raise NotImplementedError

    def log(self):
        raise NotImplementedError
