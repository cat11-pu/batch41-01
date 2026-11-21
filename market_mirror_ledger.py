class MarketMirrorLedger:
    """Cross-server auction mirror with a single source-of-truth writer."""

    def __init__(self, servers=("s1", "s2"), settlement_server="s1",
                 tax_rates=None, home_server=None):
        self.servers = tuple(servers)
        self.settlement_server = settlement_server
        self.tax_rates = dict(tax_rates or {"s1": 0.05, "s2": 0.20})
        self.home_server = home_server

    def set_player(self, server, player, currency=0.0, items=None):
        raise NotImplementedError

    def publish(self, listing_id, seller, item, price, server="s1", now=0.0):
        raise NotImplementedError

    def replicate(self, server, watermark=0, now=0.0):
        raise NotImplementedError

    def purchase(self, listing_id, buyer, buyer_server="s2",
                 expected_version=None, now=0.0):
        raise NotImplementedError

    def cancel(self, listing_id, actor, now=0.0):
        raise NotImplementedError

    def reconcile(self, server, now=0.0):
        raise NotImplementedError

    def search(self, server, query=""):
        raise NotImplementedError

    def cache_get(self, server, listing_id):
        raise NotImplementedError

    def apply_event(self, event, target_server=None, now=0.0):
        raise NotImplementedError

    def escrow_count(self, listing_id):
        raise NotImplementedError

    def inventory(self, server, player):
        raise NotImplementedError

    def balance(self, server, player):
        raise NotImplementedError
