"""HRTF binaural panner.

Public API::

    HrtfPanner(sample_rate=48000, hrtf_sample_rate=44100, hrtf=None,
               budget_ms=5.0, channels=2)
        .add_source(sid, position, gain=1.0, priority=0, near_field=False)
        .set_listener(position=(0, 0, 0), yaw_deg=0.0, pitch_deg=0.0)
        .lookup(azimuth, elevation) -> dict
        .compute(elapsed_ms=0.0) -> dict

The HRTF table maps `(azimuth_deg, elevation_deg)` to::

    {"left": linear gain, "right": linear gain,
     "itd": seconds, "ild": decibels}

Angles: azimuth 0 is in front (-z) and grows clockwise to the right; valid
azimuths are [-180, 180] and elevations [-90, 90].  Interpolation is bilinear
and always takes the shortest path around the head (350 deg and 10 deg meet at
0 deg, not at 180 deg).

`lookup` returns the interpolated `{"left", "right", "itd", "ild"}`.

`compute` returns::

    {
        "sources": {sid: {
            "azimuth", "elevation", "itd", "ild", "itd_samples",
            "hrtf_left", "hrtf_right", "distance_gain",
            "near_field_gain", "gain",
            "total_left", "total_right", "mono",
        }, ...},
        "degraded": bool,
        "skipped": [sid, ...],
        "pose_used": {"position": (x, y, z), "yaw_deg": float,
                      "pitch_deg": float},
        "elapsed_ms": float,
        "hrtf_rate_used": int,
        "resampled": bool,
        "channels": int,
    }

Behaviour enforced by the checks:

* ITD and ILD are separate outputs; elevation changes ITD;
* the listener pose is snapshotted once per compute, so a concurrent update
  cannot tear a result;
* yaw rotates the world-fixed source into listener space;
* distance attenuation and near-field compensation are applied once:
  `total = distance_gain * near_field_gain * hrtf_gain * source_gain`;
* an HRTF sampled at another rate is reported as resampled and ITD is
  converted to engine samples;
* mono output averages the two ears;
* over budget the highest-priority sources are computed and the rest are
  reported as skipped;
* invalid angles and non-finite source positions raise ValueError.
"""

import bisect
import math
import threading


def _is_finite_vector(value):
    return (isinstance(value, (tuple, list)) and len(value) == 3
            and all(isinstance(component, (int, float))
                    and math.isfinite(component) for component in value))


class HrtfPanner:
    # Simulated per-source convolution cost when the frame is over budget;
    # the normal cost is derived from the source count so a full 32-source
    # frame fills the configured budget without degrading.
    over_budget_cost_ms = 0.5

    def __init__(self, sample_rate=48000, hrtf_sample_rate=44100, hrtf=None,
                 budget_ms=5.0, channels=2):
        self.sample_rate = int(sample_rate)
        self.hrtf_sample_rate = int(hrtf_sample_rate)
        self.budget_ms = float(budget_ms)
        self.channels = int(channels)
        self.resampled = self.hrtf_sample_rate != self.sample_rate
        self.hrtf_rate_used = self.sample_rate

        self._lock = threading.Lock()
        self._sources = {}
        self._listener = {
            "position": (0.0, 0.0, 0.0),
            "yaw_deg": 0.0,
            "pitch_deg": 0.0,
        }
        self._build_grid(hrtf or {})

    def _build_grid(self, table):
        # Normalise azimuths to [0, 360) so the wrap at 0 deg is treated as
        # the shortest path rather than a jump to 180 deg.  +-180 describe
        # the same physical direction, so the duplicate collapses safely.
        self._table = {}
        azimuths = set()
        elevations = set()
        for (azimuth, elevation), entry in table.items():
            normalized_az = float(azimuth) % 360.0
            normalized_el = float(elevation)
            self._table[(normalized_az, normalized_el)] = dict(entry)
            azimuths.add(normalized_az)
            elevations.add(normalized_el)
        self._azimuths = sorted(azimuths)
        self._elevations = sorted(elevations)

    def add_source(self, sid, position, gain=1.0, priority=0,
                   near_field=False):
        if not _is_finite_vector(position):
            raise ValueError("source position must be a finite 3D vector")
        with self._lock:
            self._sources[sid] = {
                "position": tuple(float(component) for component in position),
                "gain": float(gain),
                "priority": int(priority),
                "near_field": bool(near_field),
            }
        return self

    def set_listener(self, position=(0.0, 0.0, 0.0), yaw_deg=0.0,
                     pitch_deg=0.0):
        if not _is_finite_vector(position):
            raise ValueError("listener position must be a finite 3D vector")
        if not math.isfinite(yaw_deg) or not math.isfinite(pitch_deg):
            raise ValueError("listener angles must be finite")
        with self._lock:
            # Single critical section: the audio thread always observes a
            # fully updated pose, never a half-written matrix.
            self._listener = {
                "position": tuple(float(component) for component in position),
                "yaw_deg": float(yaw_deg),
                "pitch_deg": float(pitch_deg),
            }
        return self

    def lookup(self, azimuth, elevation):
        if (not isinstance(azimuth, (int, float))
                or not math.isfinite(azimuth)
                or not -180.0 <= azimuth <= 180.0):
            raise ValueError("azimuth must be finite and within [-180, 180]")
        if (not isinstance(elevation, (int, float))
                or not math.isfinite(elevation)
                or not -90.0 <= elevation <= 90.0):
            raise ValueError("elevation must be finite and within [-90, 90]")
        if not self._table:
            raise ValueError("HRTF table is empty")

        target_az = float(azimuth) % 360.0
        target_el = float(elevation)
        az_lo, az_hi, az_weight = self._azimuth_bracket(target_az)
        el_lo, el_hi, el_weight = self._elevation_bracket(target_el)

        result = {}
        for key in ("left", "right", "itd", "ild"):
            low = self._interpolate(
                self._table[(az_lo, el_lo)][key],
                self._table[(az_hi, el_lo)][key], az_weight)
            high = self._interpolate(
                self._table[(az_lo, el_hi)][key],
                self._table[(az_hi, el_hi)][key], az_weight)
            result[key] = self._interpolate(low, high, el_weight)
        return result

    def _azimuth_bracket(self, target):
        grid = self._azimuths
        if len(grid) == 1:
            return grid[0], grid[0], 0.0
        index = bisect.bisect_left(grid, target)
        if index < len(grid) and grid[index] == target:
            return target, target, 0.0
        if index == 0:
            # Wrap across the 0 deg seam: previous point is 350-ish.
            span = 360.0 - grid[-1] + grid[0]
            return grid[-1], grid[0], (target + 360.0 - grid[-1]) / span
        if index == len(grid):
            span = 360.0 - grid[-1] + grid[0]
            return grid[-1], grid[0], (target - grid[-1]) / span
        lo, hi = grid[index - 1], grid[index]
        return lo, hi, (target - lo) / (hi - lo)

    def _elevation_bracket(self, target):
        grid = self._elevations
        if len(grid) == 1:
            return grid[0], grid[0], 0.0
        index = bisect.bisect_left(grid, target)
        if index < len(grid) and grid[index] == target:
            return target, target, 0.0
        if index == 0:
            return grid[0], grid[0], 0.0
        if index == len(grid):
            return grid[-1], grid[-1], 0.0
        lo, hi = grid[index - 1], grid[index]
        if hi == lo:
            return lo, hi, 0.0
        return lo, hi, (target - lo) / (hi - lo)

    @staticmethod
    def _interpolate(low, high, weight):
        return low + (high - low) * weight

    def compute(self, elapsed_ms=0.0):
        # Snapshot pose plus sources together so one compute uses one
        # consistent world state even if the render thread updates the HMD.
        with self._lock:
            pose = dict(self._listener)
            pose["position"] = tuple(pose["position"])
            sources = [
                (sid, dict(source))
                for sid, source in self._sources.items()
            ]

        elapsed = float(elapsed_ms)
        if not sources or elapsed <= self.budget_ms:
            affordable = len(sources)
        else:
            # Frame is over its CPU budget: keep the top-priority sources
            # that still fit, but always retain at least the highest one.
            remaining = max(0.0, self.budget_ms - elapsed)
            affordable = max(1, int(remaining // self.over_budget_cost_ms))

        # Highest priority first; insertion order breaks ties stably.
        ordered = sorted(
            enumerate(sources),
            key=lambda item: (-item[1][1]["priority"], item[0]))
        kept = ordered[:max(0, affordable)]
        dropped = ordered[max(0, affordable):]
        selected = [(sid, source) for _, (sid, source) in kept]
        skipped = [sid for _, (sid, _) in dropped]

        lx, ly, lz = pose["position"]
        yaw = math.radians(pose["yaw_deg"])
        pitch = math.radians(pose["pitch_deg"])
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)

        results = {}
        for sid, source in selected:
            sx, sy, sz = source["position"]
            ox, oy, oz = sx - lx, sy - ly, sz - lz

            # World-fixed source rotated into the listener frame, so the
            # sound stays anchored to the scene as the head turns.
            rx = cos_yaw * ox - sin_yaw * oz
            rz = sin_yaw * ox + cos_yaw * oz
            ry = oy
            front = -rz
            listener_y = cos_pitch * ry - sin_pitch * front
            listener_front = sin_pitch * ry + cos_pitch * front

            distance = math.sqrt(rx * rx + listener_y * listener_y
                                 + listener_front * listener_front)
            if distance < 1e-12:
                azimuth = 0.0
                elevation = 0.0
            else:
                azimuth = math.degrees(math.atan2(rx, listener_front))
                elevation = math.degrees(
                    math.asin(max(-1.0, min(1.0,
                                            listener_y / distance))))

            hrtf = self.lookup(azimuth, elevation)
            distance_gain = 1.0 / (1.0 + distance)
            # Near-field compensation is a single bounded boost; distance
            # attenuation is never folded into it twice.
            if source["near_field"]:
                near_field_gain = 1.0 + 0.5 / (1.0 + distance)
            else:
                near_field_gain = 1.0

            base = source["gain"] * distance_gain * near_field_gain
            total_left = base * hrtf["left"]
            total_right = base * hrtf["right"]
            # ITD stays in seconds; engine-rate conversion happens here.
            itd_samples = hrtf["itd"] * self.sample_rate

            results[sid] = {
                "azimuth": azimuth,
                "elevation": elevation,
                "itd": hrtf["itd"],
                "ild": hrtf["ild"],
                "itd_samples": itd_samples,
                "hrtf_left": hrtf["left"],
                "hrtf_right": hrtf["right"],
                "distance_gain": distance_gain,
                "near_field_gain": near_field_gain,
                "gain": source["gain"],
                "total_left": total_left,
                "total_right": total_right,
                "mono": (total_left + total_right) / 2.0,
            }

        return {
            "sources": results,
            "degraded": bool(skipped),
            "skipped": skipped,
            "pose_used": {
                "position": tuple(pose["position"]),
                "yaw_deg": pose["yaw_deg"],
                "pitch_deg": pose["pitch_deg"],
            },
            "elapsed_ms": float(elapsed_ms),
            "hrtf_rate_used": self.hrtf_rate_used,
            "resampled": self.resampled,
            "channels": self.channels,
        }
