"""Particle collision field with continuous detection (initial stub).

Public API::

    ParticleCollisionField(triangles=None, materials=None, cell_size=None,
                           max_radius=None, budget_ms=2.0, max_substeps=64)
        .add_triangle(a, b, c, material="default") -> int
        .add_particle(pid, position, velocity, radius,
                      material="default", priority=0)
        .remove_particle(pid)
        .normal_at(triangle_id, point) -> (nx, ny, nz)
        .cell_size -> float
        .step(dt, elapsed_ms=0.0, workers=1) -> dict
        .stats() -> dict

`step` returns::

    {
        "pairs": int,              # resolved particle/surface contacts
        "substeps": int,
        "positions": {pid: (x, y, z)},
        "velocities": {pid: (vx, vy, vz)},
        "degraded": bool,
        "candidates": int,         # potential contact pairs considered
        "processed": int,          # pairs actually resolved
        "processed_ids": [pid, ...],
        "skipped_ids": [pid, ...],
        "fallback_materials": int,
        "elapsed_ms": float,
    }

Behaviour enforced by the checks:

* sweep the motion in substeps, never a single previous-frame lookup;
* smooth contact normals from adjacent faces (shared edges average face
  normals; a coplanar shared edge is owned by exactly one triangle so a
  particle gets a single contact);
* cell size is at least twice the largest particle radius and an explicitly
  supplied smaller cell size is clamped;
* removed particles leave the hash immediately;
* missing material entries fall back to a finite default (friction 0.5,
  restitution 0.2) and are counted;
* over budget, resolve the highest-priority candidates first and report the
  skipped particle ids; never silently drop the whole batch;
* contact resolution runs in a deterministic pid-sorted order, so `workers`
  never changes the resulting positions or velocities;
* non-positive radii and non-finite positions/velocities raise ValueError.

`stats` returns at least {"triangles", "particles", "hash_particles"}.
"""


class ParticleCollisionField:
    def __init__(self, triangles=None, materials=None, cell_size=None,
                 max_radius=None, budget_ms=2.0, max_substeps=64):
        self._triangles = list(triangles or [])
        self._materials = dict(materials or {})
        self._requested_cell_size = cell_size
        self._max_radius = max_radius
        self.budget_ms = budget_ms
        self.max_substeps = max_substeps

    @property
    def cell_size(self):
        raise NotImplementedError

    def add_triangle(self, a, b, c, material="default"):
        raise NotImplementedError

    def add_particle(self, pid, position, velocity, radius,
                     material="default", priority=0):
        raise NotImplementedError

    def remove_particle(self, pid):
        raise NotImplementedError

    def normal_at(self, triangle_id, point):
        raise NotImplementedError

    def step(self, dt, elapsed_ms=0.0, workers=1):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
