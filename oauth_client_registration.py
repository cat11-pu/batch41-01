class DynamicClientRegistry:
    def __init__(self, allowed_scopes=None, allowed_grants=None, trusted_signature="sig-ok"):
        self.allowed_scopes = list(allowed_scopes or [])
        self.allowed_grants = list(allowed_grants or [])
        self.trusted_signature = trusted_signature

    def normalize_redirect_uri(self, uri):
        raise NotImplementedError

    def register(self, claim, now):
        raise NotImplementedError

    def describe(self, client_id, registration_token):
        raise NotImplementedError

    def rotate_credential(self, client_id, registration_token, now):
        raise NotImplementedError

    def delete(self, client_id, registration_token, now):
        raise NotImplementedError

    def issue_token(self, client_id, grant_type, scopes, now):
        raise NotImplementedError

    def refresh(self, refresh_token, now):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
