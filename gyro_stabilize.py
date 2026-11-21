"""Gyro based rolling-shutter aware video stabilization."""


class GyroStabilize:
    def __init__(self, width=3840, height=2160, fps=30, slope_smooth=0.0):
        self.width = width
        self.height = height
        self.fps = fps
        self.samples = []
        self.gaps = 0

    def add_gyro(self, t_ms, gx, gy, gz, seq=None):
        raise NotImplementedError

    def calibrate(self, frame_times_ms, gyro_samples):
        raise NotImplementedError

    def plan(self, frame_time_ms, shake_deg=0.0, budget_ms=8.0):
        raise NotImplementedError

    def estimate_rotation(self, t_ms):
        raise NotImplementedError

    def estimate_bias(self, samples):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
