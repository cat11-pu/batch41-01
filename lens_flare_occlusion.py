"""Lens flare occlusion planning.

The public API is:

    LensFlareOcclusion(viewport=(w, h), fov_deg=70.0, max_samples=8,
                       budget_ms=3.0, fade_rate=4.0, base_size=64.0)
        .add_light(light_id, position, radius=1.0, streak_stretch=1.0, gain=1.0)
        .add_occluder(min_corner, max_corner)
        .clear_occluders()
        .resize(width, height)
        .update(camera, dt=0.0, elapsed_ms=0.0) -> dict

`update` returns::

    {
        "flares": [flare, ...],       # sorted far-to-near by camera distance
        "degraded": bool,
        "sample_count": int,          # effective samples used this update
        "elapsed_ms": float,
        "notes": [str, ...],
    }

Each flare is a dict with keys: id, visible, visibility, coords (x, y) in
pixels, size (w, h) in pixels, streak (dx, dy) in pixels, distance, samples.

Rules enforced by the checks:

* the sample set always contains the light centre plus a ring of offsets on the
  light disc; a flare is visible only when every sample is unoccluded
  (conservative visibility);
* `fov_deg` is vertical: the focal length in pixels is
  (height / 2) / tan(fov / 2), and coordinates are the viewport centre plus
  (x_cam / z_cam, -y_cam / z_cam) * focal;
* the first update snaps visibility to the current raw visibility; afterwards
  visibility only moves at `fade_rate` per second (no hard cuts);
* flare size scales inversely with tan(fov / 2); width uses the viewport aspect;
* the streak is horizontal in pixel space and its length scales with aspect;
* lights with a camera-space depth of zero or less are culled;
* every instance owns its viewport buffers and rebuilds them on resize;
* an update over budget halves the sample density and reports it.
"""

import math


_REFERENCE_FOV_DEG = 70.0
_REFERENCE_HEIGHT = 720.0
_EPSILON = 1e-9


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(v):
    return math.sqrt(_dot(v, v))


def _normalize(v):
    magnitude = _length(v)
    if magnitude <= _EPSILON:
        raise ValueError("camera direction vectors must be non-zero")
    return (v[0] / magnitude, v[1] / magnitude, v[2] / magnitude)


def _segment_hits_box(start, end, min_corner, max_corner):
    """Return True when the closed segment intersects the axis-aligned box."""
    direction = (
        end[0] - start[0],
        end[1] - start[1],
        end[2] - start[2],
    )
    t_min = 0.0
    t_max = 1.0

    for axis in range(3):
        component = direction[axis]
        low = min_corner[axis]
        high = max_corner[axis]
        if abs(component) <= _EPSILON:
            if start[axis] < low or start[axis] > high:
                return False
            continue

        near = (low - start[axis]) / component
        far = (high - start[axis]) / component
        if near > far:
            near, far = far, near

        t_min = max(t_min, near)
        t_max = min(t_max, far)
        if t_min > t_max:
            return False

    return True


class LensFlareOcclusion:
    def __init__(self, viewport=(1280, 720), fov_deg=70.0, max_samples=8,
                 budget_ms=3.0, fade_rate=4.0, base_size=64.0):
        width, height = viewport
        self._validate_dimensions(width, height)
        if not math.isfinite(fov_deg) or not 0.0 < fov_deg < 180.0:
            raise ValueError("fov_deg must be between 0 and 180 degrees")
        if int(max_samples) < 1:
            raise ValueError("max_samples must be at least one")
        if not math.isfinite(budget_ms) or budget_ms <= 0.0:
            raise ValueError("budget_ms must be positive")
        if not math.isfinite(fade_rate) or fade_rate < 0.0:
            raise ValueError("fade_rate must be non-negative")
        if not math.isfinite(base_size) or base_size <= 0.0:
            raise ValueError("base_size must be positive")

        self.width = int(width)
        self.height = int(height)
        self.fov_deg = float(fov_deg)
        self.max_samples = int(max_samples)
        self.budget_ms = float(budget_ms)
        self.fade_rate = float(fade_rate)
        self.base_size = float(base_size)

        self._lights = {}
        self._light_order = []
        self._occluders = []
        self._visibility = {}
        self._target_generation = 0
        self._occlusion_buffer = None
        self._flare_target = None
        self._rebuild_targets()

    @staticmethod
    def _validate_dimensions(width, height):
        try:
            valid = (
                math.isfinite(width)
                and math.isfinite(height)
                and width >= 1
                and height >= 1
            )
        except TypeError:
            valid = False
        if not valid:
            raise ValueError("viewport dimensions must be positive")

    def _rebuild_targets(self):
        """Recreate all viewport-sized resources owned by this instance."""
        self._target_generation += 1
        self._occlusion_buffer = {
            "width": self.width,
            "height": self.height,
            "generation": self._target_generation,
            "kind": "visibility",
        }
        self._flare_target = {
            "width": self.width,
            "height": self.height,
            "generation": self._target_generation,
            "kind": "flare",
        }

    def add_light(self, light_id, position, radius=1.0, streak_stretch=1.0,
                  gain=1.0):
        if len(position) != 3:
            raise ValueError("light position must have three components")
        if radius < 0.0:
            raise ValueError("light radius must be non-negative")
        light = {
            "id": light_id,
            "position": tuple(float(value) for value in position),
            "radius": float(radius),
            "streak_stretch": float(streak_stretch),
            "gain": float(gain),
        }
        if light_id not in self._lights:
            self._light_order.append(light_id)
        self._lights[light_id] = light

    def add_occluder(self, min_corner, max_corner):
        if len(min_corner) != 3 or len(max_corner) != 3:
            raise ValueError("occluder corners must have three components")
        low = tuple(
            min(min_corner[axis], max_corner[axis]) for axis in range(3)
        )
        high = tuple(
            max(min_corner[axis], max_corner[axis]) for axis in range(3)
        )
        self._occluders.append((low, high))

    def clear_occluders(self):
        self._occluders.clear()

    def resize(self, width, height):
        self._validate_dimensions(width, height)
        self.width = int(width)
        self.height = int(height)
        self._rebuild_targets()

    def _camera_basis(self, camera):
        origin = tuple(float(value) for value in camera["position"])
        forward = _normalize(
            tuple(float(value) for value in camera["forward"])
        )
        raw_up = tuple(float(value) for value in camera.get("up", (0.0, 1.0, 0.0)))

        right = _cross(raw_up, forward)
        if _length(right) <= _EPSILON:
            fallback_up = (1.0, 0.0, 0.0)
            right = _cross(fallback_up, forward)
        right = _normalize(right)
        up = _normalize(_cross(forward, right))
        return origin, right, up, forward

    @staticmethod
    def _sample_offsets(sample_count, radius, direction):
        """Build the centre plus a ring of points on the light-facing disc."""
        if sample_count <= 1:
            return [(0.0, 0.0, 0.0)]

        helper = (0.0, 1.0, 0.0)
        if abs(_dot(helper, direction)) > 0.9:
            helper = (1.0, 0.0, 0.0)
        tangent = _normalize(_cross(helper, direction))
        bitangent = _cross(direction, tangent)

        offsets = [(0.0, 0.0, 0.0)]
        ring_samples = sample_count - 1
        for index in range(ring_samples):
            angle = 2.0 * math.pi * index / ring_samples
            cosine = math.cos(angle)
            sine = math.sin(angle)
            offsets.append((
                radius * (cosine * tangent[0] + sine * bitangent[0]),
                radius * (cosine * tangent[1] + sine * bitangent[1]),
                radius * (cosine * tangent[2] + sine * bitangent[2]),
            ))
        return offsets

    def _sample_world(self, light_position, offset, direction):
        return (
            light_position[0] + offset[0],
            light_position[1] + offset[1],
            light_position[2] + offset[2],
        )

    def update(self, camera, dt=0.0, elapsed_ms=0.0):
        origin, right, up, forward = self._camera_basis(camera)

        over_budget = elapsed_ms > self.budget_ms
        if over_budget:
            sample_count = max(1, self.max_samples // 2)
            notes = [
                "occlusion query over budget: sample density reduced "
                f"from {self.max_samples} to {sample_count}"
            ]
        else:
            sample_count = self.max_samples
            notes = []

        half_height = self.height * 0.5
        focal = half_height / math.tan(math.radians(self.fov_deg) * 0.5)
        reference_focal = (
            half_height / math.tan(math.radians(_REFERENCE_FOV_DEG) * 0.5)
        )
        aspect = self.width / float(self.height)
        size_height = self.base_size * (focal / reference_focal)
        size_width = size_height * aspect

        step = max(0.0, float(dt)) * self.fade_rate
        flares = []
        total_samples = 0

        for light_id in self._light_order:
            light = self._lights[light_id]
            relative = (
                light["position"][0] - origin[0],
                light["position"][1] - origin[1],
                light["position"][2] - origin[2],
            )
            depth = _dot(relative, forward)
            if depth <= _EPSILON:
                self._visibility.pop(light_id, None)
                continue

            distance = _length(relative)
            if distance <= _EPSILON:
                self._visibility.pop(light_id, None)
                continue

            direction = (
                relative[0] / distance,
                relative[1] / distance,
                relative[2] / distance,
            )
            offsets = self._sample_offsets(
                sample_count, light["radius"], direction
            )
            clear_samples = 0
            for offset in offsets:
                sample = self._sample_world(
                    light["position"], offset, direction
                )
                blocked = any(
                    _segment_hits_box(origin, sample, low, high)
                    for low, high in self._occluders
                )
                if not blocked:
                    clear_samples += 1

            raw_visibility = clear_samples / float(len(offsets))
            all_clear = clear_samples == len(offsets)
            previous = self._visibility.get(light_id)
            if previous is None:
                visibility = raw_visibility
            elif step <= 0.0:
                visibility = previous
            elif previous < raw_visibility:
                visibility = min(raw_visibility, previous + step)
            elif previous > raw_visibility:
                visibility = max(raw_visibility, previous - step)
            else:
                visibility = previous
            self._visibility[light_id] = visibility

            x_camera = _dot(relative, right)
            y_camera = _dot(relative, up)
            coords = (
                self.width * 0.5 + (x_camera / depth) * focal,
                self.height * 0.5 - (y_camera / depth) * focal,
            )
            streak_x = size_width * abs(light["streak_stretch"])
            flares.append({
                "id": light_id,
                "visible": all_clear and visibility > _EPSILON,
                "visibility": visibility,
                "coords": coords,
                "size": (size_width, size_height),
                "streak": (streak_x, 0.0),
                "distance": distance,
                "samples": sample_count,
            })
            total_samples += sample_count

        flares.sort(key=lambda flare: -flare["distance"])
        return {
            "flares": flares,
            "degraded": over_budget,
            "sample_count": total_samples,
            "elapsed_ms": float(elapsed_ms),
            "notes": notes,
        }
