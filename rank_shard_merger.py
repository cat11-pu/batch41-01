"""Cross-server ranking shard merger."""


class RankShardMerger:
    def __init__(self, seasons=None, max_page_size=100):
        self.seasons = list(seasons or [])
        self.max_page_size = max_page_size

    def submit(self, season, shard, character_id, score, version, watermark):
        raise NotImplementedError

    def merge(self, season, watermarks=None):
        raise NotImplementedError

    def rank(self, season):
        raise NotImplementedError

    def page(self, season, cursor=None, size=20):
        raise NotImplementedError

    def invalidate(self, season=None):
        raise NotImplementedError

    def remove(self, season, character_id):
        raise NotImplementedError
