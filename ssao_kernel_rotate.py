"""Screen-space ambient occlusion with a rotating kernel (initial stub).

Public API::

    SsaoKernelRotate(width, height, fov_deg=60.0, near=0.1, far=100.0,
                     radius=0.5, samples=8, strength=1.0, budget_ms=4.0)
        .reconstruct(u, v, depth) -> (x, y, z)
        .pixel_radius(world_radius, depth) -> float
        .rotate_kernel(sample, angle_deg) -> (x, y, z)
        .normal_from_depth(depth_map, x, y) -> (nx, ny, nz)
        .bilateral_upsample(ao_low, depth_low, depth_high, x, y) -> float
        .temporal_filter(current, history, depth, prev_depth, normal,
                         prev_normal, depth_threshold=0.1,
                         normal_threshold=0.9) -> float
        .compute(depth_map, normals=None, elapsed_ms=0.0) -> dict

Conventions:

* `u` and `v` are normalised screen coordinates in [0, 1]; `u` runs along +x
  and `v` down the screen (row 0 at v=0).
* Depth values are positive view-space distances.  `0.0` means the far plane
  or sky and must never be divided by.
* `reconstruct` returns camera space with the camera looking down -z:
  x = (2u - 1) * depth * tan(fov/2) * aspect
  y = (1 - 2v) * depth * tan(fov/2)
  z = -depth
* `pixel_radius` projects a world-space radius through the vertical focal
  length (height / 2) / tan(fov / 2).
* `rotate_kernel` rotates the x/y components around z.
* `normal_from_depth` uses central differences, except across a depth
  discontinuity where it falls back to the one-sided difference on the nearer
  surface so silhouettes keep a forward-facing normal.
* `bilateral_upsample` maps high-resolution pixel centres to low-resolution
  coordinates with `u = (x + 0.5) * low_width / high_width - 0.5` (same for v),
  then blends the four nearest low-resolution texels with weights that include
  exp(-abs(depth difference) / 0.05); background texels several metres away
  must not leak into a foreground pixel.
* `temporal_filter` returns `current` when depth or normal disagree beyond the
  thresholds, otherwise a history-weighted blend.
* `compute` returns {"ao": 2-D list, "samples": int, "degraded": bool,
  "elapsed_ms": float, "notes": [str, ...]}.  Sky pixels are 1.0, strength
  zero short-circuits without sampling, and over-budget work uses half the
  requested samples and reports it.
"""


class SsaoKernelRotate:
    def __init__(self, width, height, fov_deg=60.0, near=0.1, far=100.0,
                 radius=0.5, samples=8, strength=1.0, budget_ms=4.0):
        self.width = width
        self.height = height
        self.fov_deg = fov_deg
        self.near = near
        self.far = far
        self.radius = radius
        self.samples = samples
        self.strength = strength
        self.budget_ms = budget_ms

    def reconstruct(self, u, v, depth):
        raise NotImplementedError

    def pixel_radius(self, world_radius, depth):
        raise NotImplementedError

    def rotate_kernel(self, sample, angle_deg):
        raise NotImplementedError

    def normal_from_depth(self, depth_map, x, y):
        raise NotImplementedError

    def bilateral_upsample(self, ao_low, depth_low, depth_high, x, y):
        raise NotImplementedError

    def temporal_filter(self, current, history, depth, prev_depth, normal,
                        prev_normal, depth_threshold=0.1,
                        normal_threshold=0.9):
        raise NotImplementedError

    def compute(self, depth_map, normals=None, elapsed_ms=0.0):
        raise NotImplementedError
