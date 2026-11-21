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

import math


class SsaoKernelRotate:
    _NOISE_TILE = (
        (0.13, 0.62, 0.31, 0.87),
        (0.44, 0.06, 0.78, 0.29),
        (0.91, 0.37, 0.55, 0.70),
        (0.24, 0.83, 0.11, 0.49),
    )
    _DEPTH_SIGMA = 0.05
    _HISTORY_WEIGHT = 0.8

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
        self._tan_half_fov = math.tan(math.radians(fov_deg) * 0.5)
        self._aspect = width / height if height else 1.0

    @staticmethod
    def _valid_depth(depth):
        return math.isfinite(depth) and depth > 0.0

    def reconstruct(self, u, v, depth):
        if not self._valid_depth(depth):
            return (0.0, 0.0, 0.0)
        x = (2.0 * u - 1.0) * depth * self._tan_half_fov * self._aspect
        y = (1.0 - 2.0 * v) * depth * self._tan_half_fov
        return (x, y, -depth)

    def pixel_radius(self, world_radius, depth):
        if not self._valid_depth(depth):
            return 0.0
        focal_px = (self.height * 0.5) / self._tan_half_fov
        return world_radius * focal_px / depth

    def rotate_kernel(self, sample, angle_deg):
        angle = math.radians(angle_deg)
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        x, y, z = sample
        return (x * cos_a - y * sin_a, x * sin_a + y * cos_a, z)

    def normal_from_depth(self, depth_map, x, y):
        rows = len(depth_map)
        cols = len(depth_map[0]) if rows else 0
        x = min(max(int(x), 0), cols - 1)
        y = min(max(int(y), 0), rows - 1)
        center = depth_map[y][x]
        if not self._valid_depth(center):
            return (0.0, 0.0, 1.0)

        def depth_at(cx, cy):
            return depth_map[min(max(cy, 0), rows - 1)][min(max(cx, 0), cols - 1)]

        threshold = 0.1 * center

        left = depth_at(x - 1, y)
        right = depth_at(x + 1, y)
        if abs(right - left) > threshold:
            if self._valid_depth(left) and abs(left - center) <= abs(right - center):
                ddx = center - left
            elif self._valid_depth(right):
                ddx = right - center
            else:
                ddx = 0.0
        else:
            ddx = (right - left) * 0.5

        up = depth_at(x, y - 1)
        down = depth_at(x, y + 1)
        if abs(down - up) > threshold:
            if self._valid_depth(up) and abs(up - center) <= abs(down - center):
                ddy = center - up
            elif self._valid_depth(down):
                ddy = down - center
            else:
                ddy = 0.0
        else:
            ddy = (down - up) * 0.5

        scale = center * self._tan_half_fov * 2.0
        nx = -ddx / (scale * self._aspect / max(cols - 1, 1)) if cols > 1 else 0.0
        ny = -ddy / (scale / max(rows - 1, 1)) if rows > 1 else 0.0
        length = math.sqrt(nx * nx + ny * ny + 1.0)
        return (nx / length, ny / length, 1.0 / length)

    def bilateral_upsample(self, ao_low, depth_low, depth_high, x, y):
        low_h = len(ao_low)
        low_w = len(ao_low[0]) if low_h else 0
        high_h = len(depth_high)
        high_w = len(depth_high[0]) if high_h else 0
        if not low_w or not high_w:
            return 1.0
        u = (x + 0.5) * low_w / high_w - 0.5
        v = (y + 0.5) * low_h / high_h - 0.5
        x0 = math.floor(u)
        y0 = math.floor(v)
        fx = u - x0
        fy = v - y0
        x = min(max(int(x), 0), high_w - 1)
        y = min(max(int(y), 0), high_h - 1)
        center_depth = depth_high[y][x]
        if not self._valid_depth(center_depth):
            return 1.0

        total = 0.0
        weight_sum = 0.0
        for dy in (0, 1):
            for dx in (0, 1):
                sx = min(max(x0 + dx, 0), low_w - 1)
                sy = min(max(y0 + dy, 0), low_h - 1)
                bilinear = (fx if dx else 1.0 - fx) * (fy if dy else 1.0 - fy)
                sample_depth = depth_low[sy][sx]
                if not self._valid_depth(sample_depth):
                    continue
                depth_weight = math.exp(
                    -abs(sample_depth - center_depth) / self._DEPTH_SIGMA)
                weight = bilinear * depth_weight
                total += weight * ao_low[sy][sx]
                weight_sum += weight
        if weight_sum <= 1e-12:
            sx = min(max(int(round(u)), 0), low_w - 1)
            sy = min(max(int(round(v)), 0), low_h - 1)
            return ao_low[sy][sx]
        return total / weight_sum

    def temporal_filter(self, current, history, depth, prev_depth, normal,
                        prev_normal, depth_threshold=0.1,
                        normal_threshold=0.9):
        if abs(depth - prev_depth) > depth_threshold:
            return current
        dot = sum(a * b for a, b in zip(normal, prev_normal))
        if dot < normal_threshold:
            return current
        return (self._HISTORY_WEIGHT * history
                + (1.0 - self._HISTORY_WEIGHT) * current)

    def _kernel_sample(self, index, count):
        golden = math.pi * (3.0 - math.sqrt(5.0))
        angle = index * golden
        scale = (index + 0.5) / count
        radial = self.radius * math.sqrt(scale)
        z = self.radius * (0.1 + 0.9 * scale)
        return (radial * math.cos(angle), radial * math.sin(angle), z)

    def _project(self, pos):
        x, y, z = pos
        depth = -z
        if depth <= 0.0:
            return None
        u = (x / (depth * self._tan_half_fov * self._aspect) + 1.0) * 0.5
        v = (1.0 - y / (depth * self._tan_half_fov)) * 0.5
        if not (0.0 <= u < 1.0 and 0.0 <= v < 1.0):
            return None
        return (int(u * self.width), int(v * self.height))

    def compute(self, depth_map, normals=None, elapsed_ms=0.0):
        notes = []
        rows = len(depth_map)
        cols = len(depth_map[0]) if rows else 0

        if self.strength == 0.0:
            notes.append("strength is zero: sampling short-circuited")
            return {
                "ao": [[1.0 for _ in range(cols)] for _ in range(rows)],
                "samples": 0,
                "degraded": False,
                "elapsed_ms": elapsed_ms,
                "notes": notes,
            }

        degraded = elapsed_ms > self.budget_ms
        samples_used = self.samples
        if degraded:
            samples_used = max(1, self.samples // 2)
            notes.append(
                "over budget (%.2fms > %.2fms): samples reduced %d -> %d"
                % (elapsed_ms, self.budget_ms, self.samples, samples_used))

        ao = [[1.0 for _ in range(cols)] for _ in range(rows)]
        bias = 0.02
        for y in range(rows):
            for x in range(cols):
                depth = depth_map[y][x]
                if not self._valid_depth(depth) or depth >= self.far:
                    continue
                u = (x + 0.5) / cols
                v = (y + 0.5) / rows
                origin = self.reconstruct(u, v, depth)
                if normals is not None:
                    normal = normals[y][x]
                else:
                    normal = self.normal_from_depth(depth_map, x, y)
                noise = self._NOISE_TILE[y % 4][x % 4]
                angle = noise * 360.0
                occluded = 0
                counted = 0
                for i in range(samples_used):
                    sample = self._kernel_sample(i, samples_used)
                    sample = self.rotate_kernel(sample, angle)
                    if normal[2] < 0.0:
                        sample = (sample[0], sample[1], -sample[2])
                    pos = (origin[0] + sample[0],
                           origin[1] + sample[1],
                           origin[2] + sample[2])
                    pixel = self._project(pos)
                    if pixel is None:
                        continue
                    scene_depth = depth_map[pixel[1]][pixel[0]]
                    if not self._valid_depth(scene_depth):
                        continue
                    counted += 1
                    if scene_depth < -pos[2] - bias:
                        occluded += 1
                if counted == 0:
                    continue
                occlusion = occluded / counted
                ao[y][x] = max(0.0, min(1.0, 1.0 - self.strength * occlusion))

        return {
            "ao": ao,
            "samples": samples_used,
            "degraded": degraded,
            "elapsed_ms": elapsed_ms,
            "notes": notes,
        }
