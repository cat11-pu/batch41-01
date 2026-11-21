"""Beat detection, BPM octave correction and beat grid generation."""

import copy
import statistics
import threading
import time


class BeatGridAlign:
    def __init__(self, sample_rate=44100, min_bpm=60.0, max_bpm=200.0):
        self.sample_rate = sample_rate
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.result = {}
        self._lock = threading.Lock()
        self._phase = 0
        self._period = 0
        self._latency_offset = 0
        self._duration = 0

    def detect_onsets(self, samples):
        if not samples:
            return []
        peak = max(abs(s) for s in samples)
        if peak == 0:
            return []
        threshold = max(1, peak // 10)
        onsets = []
        prev_above = abs(samples[0]) >= threshold
        if prev_above:
            onsets.append(0)
        for i in range(1, len(samples)):
            above = abs(samples[i]) >= threshold
            if above and not prev_above:
                onsets.append(i)
            prev_above = above
        return onsets

    def analyze(self, samples, latency_ms=0.0):
        started = time.perf_counter()
        if len(samples) < max(256, self.sample_rate // 40):
            raise ValueError("audio too short to analyze")
        self._duration = len(samples)
        self._latency_offset = int(round(latency_ms * self.sample_rate / 1000.0))
        onsets = self.detect_onsets(samples)
        if not onsets:
            result = {
                "silent": True,
                "bpm": None,
                "phase_samples": 0,
                "downbeat": 0,
                "segments": [],
                "onsets": [],
                "latency_ms": latency_ms,
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            }
            with self._lock:
                self.result = result
                self._period = 0
                self._phase = 0
            return result

        intervals = [b - a for a, b in zip(onsets, onsets[1:])]
        segments = self._build_segments(onsets, intervals)
        if intervals:
            period = statistics.median(intervals)
        else:
            period = segments[0]["period_samples"] if segments else 0
        bpm = self._octave_correct(60.0 * self.sample_rate / period) if period else None
        phase = onsets[0]
        downbeat = self._vote_downbeat(samples, onsets)
        self._phase = phase
        self._period = int(round(period)) if period else 0
        result = {
            "silent": False,
            "bpm": bpm,
            "phase_samples": phase,
            "downbeat": downbeat,
            "segments": segments,
            "onsets": list(onsets),
            "latency_ms": latency_ms,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }
        with self._lock:
            self.result = result
        return result

    def _octave_correct(self, bpm):
        while bpm > self.max_bpm:
            bpm /= 2.0
        while bpm < self.min_bpm:
            bpm *= 2.0
        return bpm

    def _build_segments(self, onsets, intervals):
        if not intervals:
            return []
        groups = [[intervals[0]]]
        group_starts = [onsets[0]]
        for i, interval in enumerate(intervals[1:], start=1):
            mean = sum(groups[-1]) / len(groups[-1])
            if abs(interval - mean) > mean * 0.15:
                groups.append([interval])
                group_starts.append(onsets[i])
            else:
                groups[-1].append(interval)
        segments = []
        for group, start in zip(groups, group_starts):
            period = statistics.median(group)
            segments.append({
                "start_sample": start,
                "period_samples": period,
                "bpm": self._octave_correct(60.0 * self.sample_rate / period),
            })
        return segments

    def _vote_downbeat(self, samples, onsets):
        if len(onsets) < 4:
            return 0
        scores = [0, 0, 0, 0]
        for beat_index, onset in enumerate(onsets):
            scores[beat_index % 4] += abs(samples[onset])
        return scores.index(max(scores))

    def beat_times(self, duration_samples=None):
        if duration_samples is None:
            duration_samples = self._duration
        if not self._period:
            return []
        start = self._phase + self._latency_offset
        return list(range(start, duration_samples + 1, self._period))

    def resample(self, samples, source_rate, target_rate):
        if source_rate == target_rate:
            return list(samples)
        out_len = round(len(samples) * target_rate / source_rate)
        out = []
        last = len(samples) - 1
        for i in range(out_len):
            pos = i * source_rate / target_rate
            j = int(pos)
            frac = pos - j
            if j >= last:
                out.append(samples[last])
            else:
                out.append(int(round(samples[j] * (1.0 - frac) + samples[j + 1] * frac)))
        return out

    def mixdown(self, left, right):
        return [(a + b) // 2 for a, b in zip(left, right)]

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.result)

    def stats(self):
        snap = self.snapshot()
        return {
            "bpm": snap.get("bpm"),
            "phase_samples": snap.get("phase_samples", 0),
            "segments": len(snap.get("segments", [])),
            "elapsed_ms": snap.get("elapsed_ms", 0.0),
        }
