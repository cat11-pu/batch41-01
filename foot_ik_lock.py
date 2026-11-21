"""Two-point foot IK with pelvis adaptation and local-space locking."""


class FootIkLock:
    def __init__(self, leg_length=1.0, pelvis_max_drop=0.4,
                 slope_smooth_deg_per_s=90.0):
        self.leg_length = leg_length
        self.pelvis_max_drop = pelvis_max_drop
        self.slope_smooth_deg_per_s = slope_smooth_deg_per_s
        self._locked = {"left": False, "right": False}
        self._last_pitch = {"left": 0.0, "right": 0.0}
        self._pelvis_writes = 0

    def solve(self, feet, character, ground, dt=1.0 / 60.0):
        raise NotImplementedError

    def release(self, foot=None, reason="hit"):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
