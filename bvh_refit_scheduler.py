"""BVH dirty/refit scheduling (initial stub).

Public API::

    BvhRefitScheduler(budget_ms=2.0, leaf_size=4)
        .build(primitives) -> revision
        .mark_dirty(pid, priority=0)
        .update_primitive(pid, verts=None, translate=None)   # marks dirty
        .refit(elapsed_ms=0.0) -> dict
        .query(origin, direction, revision=None) -> [{"id", "t"}, ...]
        .remove(pid)
        .compact() -> dict
        .stats() -> dict
        .root_bounds(revision=None) -> ((minx, miny, minz), (maxx, maxy, maxz))

A primitive is::

    {"id": hashable, "verts": [[x, y, z], [x, y, z], [x, y, z]],
     "translate": [x, y, z]}

Behaviour enforced by the checks:

* non-finite vertices raise ValueError at build time;
* zero-area triangles are pruned and counted;
* dirty marks climb to every ancestor and bounds are merged bottom-up;
* a refit publishes a new revision while the previous revision stays
  queryable with the old geometry (double buffering); `updated` counts dirty
  primitives that were refitted this call;
* over budget, the highest-priority dirty primitives are updated first and the
  rest are reported as skipped, never a whole-frame abort;
* removals are compacted on request and remove the primitive from queries;
* rays whose origin lies inside the root bounds only report positive hits;
* instance translations mark the owning primitive and its ancestors dirty.
"""


class BvhRefitScheduler:
    def __init__(self, budget_ms=2.0, leaf_size=4):
        self.budget_ms = budget_ms
        self.leaf_size = leaf_size

    def build(self, primitives):
        raise NotImplementedError

    def mark_dirty(self, pid, priority=0):
        raise NotImplementedError

    def update_primitive(self, pid, verts=None, translate=None):
        raise NotImplementedError

    def refit(self, elapsed_ms=0.0):
        raise NotImplementedError

    def query(self, origin, direction, revision=None):
        raise NotImplementedError

    def remove(self, pid):
        raise NotImplementedError

    def compact(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def root_bounds(self, revision=None):
        raise NotImplementedError
