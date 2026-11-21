"""FFT-style ocean surface update and sampling (initial stub).

Public API::

    WaterSurfaceFft(size=(64, 64), world_size=64.0, layers=3, wind=5.0,
                    budget_ms=3.0)
        .update(time, camera=(0.0, 0.0), elapsed_ms=0.0) -> dict
        .height_at(x, y, revision=None) -> float
        .height_at_lod(x, y, lod) -> float
        .normal_at(x, y) -> (nx, ny, nz)
        .normal_from_heights(h_left, h_right, h_down, h_up, dx, dz) -> tuple
        .foam_at(x, y) -> float
        .buoyancy(bbox, samples=5) -> dict
        .set_reflection(mode)
        .snapshot() -> dict

`update` returns::

    {
        "revision": int,
        "height_range": (min_height, max_height),
        "degraded": bool,
        "layers_used": int,
        "elapsed_ms": float,
        "reflection": {"sources": ["planar"] or ["ssr"], "intensity": float},
    }

Behaviour enforced by the checks:

* the wave field is periodic with `world_size`, not with an arbitrary smaller
  tile;
* `normal_from_heights` uses central differences divided by `2 * dx` (and
  `2 * dz`), so changing the sample spacing changes the slope correctly;
* foam grows with wind strength and is higher on crests than in troughs;
* buoyancy samples the hull bounding box at `samples` points and reports the
  sample count and submerged fraction;
* LOD levels share the same boundary height field;
* only one reflection source is active at a time;
* zero wind degenerates to still water without dividing by zero;
* moving the camera never changes the world-space wave phase;
* revisions keep their height field, so an older revision can still be sampled
  after an update (double buffering);
* over budget reduces the layer count and reports it.
"""


class WaterSurfaceFft:
    def __init__(self, size=(64, 64), world_size=64.0, layers=3, wind=5.0,
                 budget_ms=3.0):
        self.size = size
        self.world_size = world_size
        self.layers = layers
        self.wind = wind
        self.budget_ms = budget_ms

    def update(self, time, camera=(0.0, 0.0), elapsed_ms=0.0):
        raise NotImplementedError

    def height_at(self, x, y, revision=None):
        raise NotImplementedError

    def height_at_lod(self, x, y, lod):
        raise NotImplementedError

    def normal_at(self, x, y):
        raise NotImplementedError

    def normal_from_heights(self, h_left, h_right, h_down, h_up, dx, dz):
        raise NotImplementedError

    def foam_at(self, x, y):
        raise NotImplementedError

    def buoyancy(self, bbox, samples=5):
        raise NotImplementedError

    def set_reflection(self, mode):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
