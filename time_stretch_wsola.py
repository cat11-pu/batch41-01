"""WSOLA time stretching with an explicit planning step."""


class TimeStretchWsola:
    def __init__(self, sample_rate=48000, channels=2, window_ms=20):
        self.sample_rate = sample_rate
        self.channels = channels
        self.window_ms = window_ms

    def plan(self, samples, rate_num, rate_den=1, resample_to=None):
        raise NotImplementedError

    def process(self, samples, rate_num, rate_den=1, plan=None, resample_to=None):
        raise NotImplementedError

    def detect_transients(self, samples):
        raise NotImplementedError

    def seek(self, sample_index):
        raise NotImplementedError

    def push(self, samples):
        raise NotImplementedError

    def pull(self, count):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
