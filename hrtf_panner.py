"""HRTF binaural panner (initial stub).

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


class HrtfPanner:
    def __init__(self, sample_rate=48000, hrtf_sample_rate=44100, hrtf=None,
                 budget_ms=5.0, channels=2):
        self.sample_rate = sample_rate
        self.hrtf_sample_rate = hrtf_sample_rate
        self.hrtf = dict(hrtf or {})
        self.budget_ms = budget_ms
        self.channels = channels

    def add_source(self, sid, position, gain=1.0, priority=0,
                   near_field=False):
        raise NotImplementedError

    def set_listener(self, position=(0.0, 0.0, 0.0), yaw_deg=0.0,
                     pitch_deg=0.0):
        raise NotImplementedError

    def lookup(self, azimuth, elevation):
        raise NotImplementedError

    def compute(self, elapsed_ms=0.0):
        raise NotImplementedError
