"""Motion matching feature index.

Clip input: ``{"id", "features": {"speed", "facing", "angular"},
"length", "mirror"}``.

Contract used by the tests
--------------------------
* ``build`` replaces the library, ``insert`` adds one clip, ``remove`` deletes
  one.  Both bump ``version`` (build sets version 1).
* Each feature dimension is min-max normalized with the library's own min and
  max (range floored at 1e-6).  Query features use the same scale, so the
  returned ``cost`` is a distance in normalized space.
* A clip with ``mirror=True`` flips the sign of ``facing`` and ``angular``
  before normalization; the result keeps ``"mirror": True``.
* ``query(features, current=None, limit=1, query_length=None, version=None)``:
  - candidates never include ``current``;
  - with ``current`` the current clip's cost is computed first; if the best
    candidate improves it by less than ``hysteresis`` the result is a single
    sticky entry for the current clip (``"sticky": True``);
  - otherwise results are sorted by cost, capped at ``limit``;
  - every result has exactly ``id, cost, weight, mirror, sticky``;
  - ``weight = min(query_length, length) / length`` when ``query_length`` is
    given, else 1.0;
  - an empty library returns ``[]``; a single-clip library queried with that
    clip as ``current`` returns ``[]``;
  - ``version=v`` reads the library as of version v.
* ``predict(features, horizon=0.2, previous_speed=0.0)`` returns
  ``{"distance": features["speed"] * horizon}`` (the current speed wins, so a
  hard stop predicts zero).
* ``stats()`` returns ``{"clips", "version", "last_query_candidates"}``.
"""


import math
import threading

_FEATURE_DIMS = ("speed", "facing", "angular")
_MIN_RANGE = 1e-6


class _Snapshot:
    """Immutable, fully-built view of the library at one version.

    Mirrored clips flip the sign of ``facing`` and ``angular`` before
    normalization; each dimension is then min-max normalized with the
    library's own min and max (range floored at 1e-6).
    """

    __slots__ = ("clips", "vectors", "mins", "ranges", "index_by_id")

    def __init__(self, clips):
        self.clips = list(clips)
        raw = []
        for clip in self.clips:
            feats = clip["features"]
            sign = -1.0 if clip.get("mirror") else 1.0
            raw.append((feats["speed"], sign * feats["facing"], sign * feats["angular"]))
        if raw:
            self.mins = tuple(min(vec[axis] for vec in raw) for axis in range(3))
            self.ranges = tuple(
                max(max(vec[axis] for vec in raw) - self.mins[axis], _MIN_RANGE)
                for axis in range(3)
            )
        else:
            self.mins = (0.0, 0.0, 0.0)
            self.ranges = (_MIN_RANGE, _MIN_RANGE, _MIN_RANGE)
        self.vectors = [
            tuple((vec[axis] - self.mins[axis]) / self.ranges[axis] for axis in range(3))
            for vec in raw
        ]
        self.index_by_id = {clip["id"]: i for i, clip in enumerate(self.clips)}


def _entry(snapshot, index, cost, query_length, sticky):
    clip = snapshot.clips[index]
    length = clip.get("length", 1.0)
    weight = 1.0 if query_length is None else min(query_length, length) / length
    return {
        "id": clip["id"],
        "cost": float(cost),
        "weight": weight,
        "mirror": bool(clip.get("mirror", False)),
        "sticky": sticky,
    }


class MotionMatchIndex:
    def __init__(self, hysteresis=0.1):
        self.hysteresis = hysteresis
        self._version = 0
        self._snapshots = {0: _Snapshot([])}
        self._last_query_candidates = 0
        self._write_lock = threading.Lock()

    def build(self, clips):
        with self._write_lock:
            self._version = 1
            self._snapshots = {1: _Snapshot(clips)}

    def insert(self, clip):
        with self._write_lock:
            current = self._snapshots[self._version].clips
            clips = [c for c in current if c["id"] != clip["id"]]
            clips.append(clip)
            self._version += 1
            self._snapshots[self._version] = _Snapshot(clips)

    def remove(self, clip_id):
        with self._write_lock:
            current = self._snapshots[self._version].clips
            clips = [c for c in current if c["id"] != clip_id]
            if len(clips) == len(current):
                raise KeyError(clip_id)
            self._version += 1
            self._snapshots[self._version] = _Snapshot(clips)

    def query(self, features, current=None, limit=1, query_length=None, version=None):
        snapshot = self._snapshots[self._version if version is None else version]
        count = len(snapshot.clips)
        if count == 0:
            self._last_query_candidates = 0
            return []
        query_vec = tuple(
            (features[dim] - snapshot.mins[axis]) / snapshot.ranges[axis]
            for axis, dim in enumerate(_FEATURE_DIMS)
        )
        current_index = snapshot.index_by_id.get(current) if current is not None else None
        current_cost = None
        candidates = []
        for i in range(count):
            vec = snapshot.vectors[i]
            cost = math.sqrt(
                (query_vec[0] - vec[0]) ** 2
                + (query_vec[1] - vec[1]) ** 2
                + (query_vec[2] - vec[2]) ** 2
            )
            if i == current_index:
                current_cost = cost
            else:
                candidates.append((cost, i))
        self._last_query_candidates = len(candidates)
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0])
        if current_cost is not None:
            best_cost = candidates[0][0]
            if best_cost < current_cost and current_cost - best_cost < self.hysteresis:
                return [
                    _entry(snapshot, current_index, current_cost, query_length, sticky=True)
                ]
        return [
            _entry(snapshot, i, cost, query_length, sticky=False)
            for cost, i in candidates[:limit]
        ]

    def predict(self, features, horizon=0.2, previous_speed=0.0):
        return {"distance": features["speed"] * horizon}

    def version(self):
        return self._version

    def stats(self):
        return {
            "clips": len(self._snapshots[self._version].clips),
            "version": self._version,
            "last_query_candidates": self._last_query_candidates,
        }
