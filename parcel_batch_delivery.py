class ParcelBatchDelivery:
    """Batch parcel delivery with idempotent receipts and a player ledger."""

    def __init__(self, mail_capacity=5, expire_seconds=604800, home_server="s1"):
        self.mail_capacity = mail_capacity
        self.expire_seconds = expire_seconds
        self.home_server = home_server

    def plan(self, batch_id, entries, now=0.0):
        raise NotImplementedError

    def dispatch(self, batch_id, now=0.0, limit=None):
        raise NotImplementedError

    def retry(self, batch_id, now=0.0, limit=None):
        raise NotImplementedError

    def revoke(self, batch_id, actor="system", now=0.0):
        raise NotImplementedError

    def audit(self, batch_id=None):
        raise NotImplementedError

    def ledger(self, player_id):
        raise NotImplementedError

    def claim(self, mail_id, items=None, now=0.0):
        raise NotImplementedError

    def delete_mail(self, mail_id, now=0.0):
        raise NotImplementedError

    def inbox(self, player_id, include_deleted=False):
        raise NotImplementedError

    def reconcile(self, batch_id):
        raise NotImplementedError
