"""FFT-style ocean surface update and sampling.

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


import math
import threading


GRAVITY = 9.81
WIND_REFERENCE = 10.0
FOAM_WIND_SATURATION = 12.0
FOAM_HEIGHT_REFERENCE = 1.0
MAX_REVISIONS = 8
REFLECTION_INTENSITY = {"planar": 0.8, "ssr": 0.6}

# Directional wave modes (nx, nz) per layer. Every wave vector is an integer
# multiple of 2*pi/world_size, so the summed field repeats exactly at
# world_size. No mode index is a multiple of 4, which rules out a spurious
# quarter-tile repetition. All layers share the same world-space phase basis
# (zero phase offset), so their crests stay aligned instead of cancelling.
WAVE_MODES = (
    ((1, 0), (0, 1)),
    ((2, 1), (1, 2)),
    ((3, 2), (2, 3)),
)
LAYER_AMPLITUDES = (0.55, 0.30, 0.18)


class WaterSurfaceFft:
    def __init__(self, size=(64, 64), world_size=64.0, layers=3, wind=5.0,
                 budget_ms=3.0):
        self.size = size
        self.world_size = world_size
        self.layers = layers
        self.wind = wind
        self.budget_ms = budget_ms
        self._nx, self._ny = size
        self._dx = world_size / self._nx
        self._dz = world_size / self._ny

        # Double buffering: update writes the back buffer and publishes it
        # with a single reference swap, so reader threads never observe a
        # half-written (torn) height field.
        self._front = None
        self._back = None
        self._lock = threading.Lock()
        self._revision = 0
        self._revisions = {}

        self._reflection = "planar"
        self._layers_used = layers
        self._degraded = False
        self._height_range = (0.0, 0.0)
        self._elapsed_ms = 0.0
        self._time = 0.0
        self._camera = (0.0, 0.0)

    def _build_components(self, layers_used):
        wind_scale = max(self.wind, 0.0) / WIND_REFERENCE
        components = []
        for layer in range(layers_used):
            mode_row = WAVE_MODES[min(layer, len(WAVE_MODES) - 1)]
            amplitude = LAYER_AMPLITUDES[
                min(layer, len(LAYER_AMPLITUDES) - 1)
            ]
            for mode_x, mode_z in mode_row:
                kx = 2.0 * math.pi * mode_x / self.world_size
                kz = 2.0 * math.pi * mode_z / self.world_size
                wave_number = math.hypot(kx, kz)
                omega = math.sqrt(GRAVITY * wave_number)
                components.append((wind_scale * amplitude, kx, kz, omega))
        return components

    @staticmethod
    def _wave_height(x, z, time, components):
        height = 0.0
        for amplitude, kx, kz, omega in components:
            height += amplitude * math.cos(kx * x + kz * z - omega * time)
        return height

    def _new_grid(self):
        return [0.0] * (self._nx * self._ny)

    def _select_grid(self, revision):
        if revision is None:
            return self._front
        return self._revisions[revision][0]

    def _sample(self, grid, x, y):
        # Periodic wrap keeps the field tiled seamlessly at world_size.
        grid_x = (x % self.world_size) / self._dx
        grid_z = (y % self.world_size) / self._dz

        i0 = int(math.floor(grid_x))
        j0 = int(math.floor(grid_z))
        tx = grid_x - i0
        tz = grid_z - j0
        i1 = (i0 + 1) % self._nx
        j1 = (j0 + 1) % self._ny
        i0 %= self._nx
        j0 %= self._ny

        h00 = grid[j0 * self._nx + i0]
        h01 = grid[j0 * self._nx + i1]
        h10 = grid[j1 * self._nx + i0]
        h11 = grid[j1 * self._nx + i1]
        top = h00 + (h01 - h00) * tx
        bottom = h10 + (h11 - h10) * tx
        return top + (bottom - top) * tz

    def update(self, time, camera=(0.0, 0.0), elapsed_ms=0.0):
        self._degraded = elapsed_ms > self.budget_ms
        if self._degraded:
            layers_used = max(
                1, int(self.layers * self.budget_ms / elapsed_ms)
            )
        else:
            layers_used = self.layers
        self._layers_used = min(layers_used, self.layers)

        components = self._build_components(self._layers_used)

        # A buffer still pinned by a retained revision must never be
        # overwritten; allocate a fresh back buffer in that case.
        retained = {id(grid) for grid, _ in self._revisions.values()}
        if self._back is None or id(self._back) in retained:
            self._back = self._new_grid()
        grid = self._back

        minimum = math.inf
        maximum = -math.inf
        index = 0
        for j in range(self._ny):
            world_z = j * self._dz
            for i in range(self._nx):
                height = self._wave_height(
                    i * self._dx, world_z, time, components
                )
                grid[index] = height
                if height < minimum:
                    minimum = height
                if height > maximum:
                    maximum = height
                index += 1

        metadata = {"time": time, "layers_used": self._layers_used}
        with self._lock:
            # Single atomic reference swap publishes the new revision.
            self._front, self._back = self._back, self._front
            self._revision += 1
            revision = self._revision
            self._revisions[revision] = (self._front, metadata)
            for stale in sorted(self._revisions)[:-MAX_REVISIONS]:
                del self._revisions[stale]

        self._time = time
        self._camera = camera
        self._elapsed_ms = elapsed_ms
        self._height_range = (minimum, maximum)

        return {
            "revision": revision,
            "height_range": self._height_range,
            "degraded": self._degraded,
            "layers_used": self._layers_used,
            "elapsed_ms": elapsed_ms,
            "reflection": {
                "sources": [self._reflection],
                "intensity": REFLECTION_INTENSITY[self._reflection],
            },
        }

    def height_at(self, x, y, revision=None):
        grid = self._select_grid(revision)
        if grid is None:
            return 0.0
        return self._sample(grid, x, y)

    def height_at_lod(self, x, y, lod):
        # Every LOD shares the same published boundary height field, so LOD
        # transitions stay continuous and never open a seam.
        return self.height_at(x, y)

    def normal_at(self, x, y):
        return self.normal_from_heights(
            self.height_at(x - self._dx, y),
            self.height_at(x + self._dx, y),
            self.height_at(x, y - self._dz),
            self.height_at(x, y + self._dz),
            self._dx,
            self._dz,
        )

    def normal_from_heights(self, h_left, h_right, h_down, h_up, dx, dz):
        # Central differences divided by the real world spacing.
        slope_x = (h_right - h_left) / (2.0 * dx)
        slope_z = (h_up - h_down) / (2.0 * dz)
        nx, ny, nz = -slope_x, 1.0, -slope_z
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            return (0.0, 1.0, 0.0)
        return (nx / length, ny / length, nz / length)

    def foam_at(self, x, y):
        height = self.height_at(x, y)
        wind_term = min(max(self.wind, 0.0) / FOAM_WIND_SATURATION, 1.0)
        crest = min(
            max(0.5 + 0.5 * height / FOAM_HEIGHT_REFERENCE, 0.0), 1.0
        )
        return min(max(wind_term * (0.4 + 0.6 * crest), 0.0), 1.0)

    def buoyancy(self, bbox, samples=5):
        x0, y0, width, depth = bbox
        if samples <= 1:
            offsets = (0.5,)
            weights = (1.0,)
        else:
            offsets = tuple(index / (samples - 1) for index in range(samples))
            raw_weights = tuple(
                0.5 if index in (0, samples - 1) else 1.0
                for index in range(samples)
            )
            weight_sum = sum(raw_weights)
            weights = tuple(weight / weight_sum for weight in raw_weights)

        center_z = y0 + depth * 0.5
        weighted_height = 0.0
        submerged = 0.0
        for offset, weight in zip(offsets, weights):
            sample_x = x0 + width * offset
            height = self.height_at(sample_x, center_z)
            weighted_height += weight * height
            if height > 0.0:
                submerged += weight

        return {
            "samples": samples,
            "submerged": submerged,
            "height": weighted_height,
        }

    def set_reflection(self, mode):
        if mode not in REFLECTION_INTENSITY:
            raise ValueError("unknown reflection mode: %r" % (mode,))
        self._reflection = mode

    def snapshot(self):
        return {
            "revision": self._revision,
            "size": self.size,
            "world_size": self.world_size,
            "wind": self.wind,
            "layers": self.layers,
            "layers_used": self._layers_used,
            "degraded": self._degraded,
            "budget_ms": self.budget_ms,
            "elapsed_ms": self._elapsed_ms,
            "time": self._time,
            "camera": self._camera,
            "reflection": self._reflection,
            "height_range": self._height_range,
            "buffers": 2,
        }
