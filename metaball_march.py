"""Metaball field evaluation and marching-cubes polygonization.

Contract used by the tests
--------------------------
* Field of one ball (center c, radius r, strength s):
  ``s * (1 - |p - c|^2 / r^2)`` when ``|p - c| < r``, otherwise 0.
  The field is the sum over all balls.
* ``add_ball`` rejects a radius that is not a finite number > 0 and a center
  or strength that is not finite; it returns the ball index.
* ``iso`` defaults to 0.5.  A point is inside when
  ``field > iso + tolerance``; values inside the tolerance band around iso
  are consistently treated as outside, so boundary thresholds are stable and
  a near-zero threshold still produces a mesh.
* ``polygonize(bounds_min, bounds_max)`` returns
  ``{"vertices", "indices", "normals", "stats"}``.
* Vertices on shared edges are welded; the vertex list contains no
  duplicates.  Indices are non-negative ints inside range and a multiple of
  three in count.
* Normals come from central differences of ``field`` and are unit length,
  pointing outwards.
* Adaptive depth: neighbouring cells differ by at most one level;
  ``stats["max_depth_delta"]`` reports that maximum.
* ``stats`` is ``{"balls", "vertices", "indices", "cells",
  "max_depth_delta", "estimated_ms", "truncated"}``.  Each cell sample costs
  ``sample_cost_ms`` (default 0.002) and ``estimated_ms`` must stay within
  ``budget_ms``; the depth is reduced instead of truncating the mesh, so
  ``truncated`` is always False.
* Repeated calls on one instance return identical geometry, and concurrent
  calls are deterministic.
"""


class MetaballMarch:
    def __init__(self, iso=0.5, max_depth=3, budget_ms=8.0, tolerance=1e-6, sample_cost_ms=0.002):
        self.iso = iso
        self.max_depth = max_depth
        self.budget_ms = budget_ms
        self.tolerance = tolerance
        self.sample_cost_ms = sample_cost_ms

    def add_ball(self, center, radius, strength=1.0):
        raise NotImplementedError

    def field(self, point):
        raise NotImplementedError

    def polygonize(self, bounds_min, bounds_max, budget_ms=None):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
