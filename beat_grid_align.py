"""Beat detection, BPM octave correction and beat grid generation."""


class BeatGridAlign:
    def __init__(self, sample_rate=44100, min_bpm=60.0, max_bpm=200.0):
        self.sample_rate = sample_rate
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.result = {}

    def detect_onsets(self, samples):
        raise NotImplementedError

    def analyze(self, samples, latency_ms=0.0):
        raise NotImplementedError

    def beat_times(self, duration_samples=None):
        raise NotImplementedError

    def resample(self, samples, source_rate, target_rate):
        raise NotImplementedError

    def mixdown(self, left, right):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
