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


class MotionMatchIndex:
    def __init__(self, hysteresis=0.1):
        self.hysteresis = hysteresis

    def build(self, clips):
        raise NotImplementedError

    def insert(self, clip):
        raise NotImplementedError

    def remove(self, clip_id):
        raise NotImplementedError

    def query(self, features, current=None, limit=1, query_length=None, version=None):
        raise NotImplementedError

    def predict(self, features, horizon=0.2, previous_speed=0.0):
        raise NotImplementedError

    def version(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
