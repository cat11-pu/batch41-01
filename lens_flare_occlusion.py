"""Lens flare occlusion planning (initial stub).

All planning entry points are intentionally unimplemented.  The public API is:

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


class LensFlareOcclusion:
    def __init__(self, viewport=(1280, 720), fov_deg=70.0, max_samples=8,
                 budget_ms=3.0, fade_rate=4.0, base_size=64.0):
        self.width, self.height = viewport
        self.fov_deg = fov_deg
        self.max_samples = max_samples
        self.budget_ms = budget_ms
        self.fade_rate = fade_rate
        self.base_size = base_size

    def add_light(self, light_id, position, radius=1.0, streak_stretch=1.0,
                  gain=1.0):
        raise NotImplementedError

    def add_occluder(self, min_corner, max_corner):
        raise NotImplementedError

    def clear_occluders(self):
        raise NotImplementedError

    def resize(self, width, height):
        raise NotImplementedError

    def update(self, camera, dt=0.0, elapsed_ms=0.0):
        raise NotImplementedError
