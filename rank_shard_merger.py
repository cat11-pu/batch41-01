"""Cross-server ranking shard merger."""


class RankShardMerger:
    def __init__(self, seasons=None, max_page_size=100):
        self.seasons = list(seasons or [])
        self.max_page_size = max_page_size
        self._shards = {}
        self._removed = {}
        self._shard_watermarks = {}
        self._version = 0
        self._rank_cache = {}

    def _season_shards(self, season):
        return self._shards.setdefault(season, {})

    def _bump_version(self):
        self._version += 1
        return self._version

    def submit(self, season, shard, character_id, score, version, watermark):
        shards = self._season_shards(season)
        records = shards.setdefault(shard, {})
        existing = records.get(character_id)
        if existing is not None and version < existing["version"]:
            return {"accepted": False, "reason": "stale"}
        records[character_id] = {
            "character_id": character_id,
            "score": score,
            "version": version,
        }
        self._removed.setdefault(season, set()).discard(character_id)
        watermarks = self._shard_watermarks.setdefault(season, {})
        watermarks[shard] = max(watermarks.get(shard, 0), watermark)
        self._bump_version()
        return {"accepted": True}

    def merge(self, season, watermarks=None):
        shards = self._season_shards(season)
        shard_watermarks = self._shard_watermarks.setdefault(season, {})
        if watermarks:
            for shard, required in watermarks.items():
                if required > shard_watermarks.get(shard, 0):
                    raise ValueError(
                        "shard %s has not reached watermark %s" % (shard, required)
                    )
        merged = self._merged_entries(season)
        return {"merged": len(merged), "shards": sorted(shards)}

    def rank(self, season):
        cached = self._rank_cache.get(season)
        if cached is not None and cached[0] == self._version:
            return cached[1]
        entries = self._merged_entries(season)
        result = {"version": self._version, "entries": entries}
        self._rank_cache[season] = (self._version, result)
        return result

    def _merged_entries(self, season):
        removed = self._removed.setdefault(season, set())
        best = {}
        for records in self._season_shards(season).values():
            for character_id, record in records.items():
                if character_id in removed:
                    continue
                current = best.get(character_id)
                if current is None or record["version"] > current["version"]:
                    best[character_id] = record
        ordered = sorted(
            best.values(), key=lambda record: (-record["score"], record["character_id"])
        )
        return [
            {
                "rank": index + 1,
                "character_id": record["character_id"],
                "score": record["score"],
                "version": record["version"],
            }
            for index, record in enumerate(ordered)
        ]

    def page(self, season, cursor=None, size=20):
        size = max(1, min(size, self.max_page_size))
        entries = self.rank(season)["entries"]
        start = 0
        if cursor is not None:
            positions = {
                entry["character_id"]: index for index, entry in enumerate(entries)
            }
            if cursor not in positions:
                raise ValueError("unknown cursor: %s" % cursor)
            start = positions[cursor] + 1
        items = entries[start : start + size]
        next_cursor = None
        if start + size < len(entries) and items:
            next_cursor = items[-1]["character_id"]
        return {"items": items, "next_cursor": next_cursor, "size": size}

    def invalidate(self, season=None):
        if season is None:
            self._rank_cache.clear()
        else:
            self._rank_cache.pop(season, None)
        return self._bump_version()

    def remove(self, season, character_id):
        for records in self._season_shards(season).values():
            records.pop(character_id, None)
        self._removed.setdefault(season, set()).add(character_id)
        self._bump_version()
        return {"removed": True}
