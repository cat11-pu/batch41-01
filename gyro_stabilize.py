"""Gyro based rolling-shutter aware video stabilization."""

from __future__ import annotations

import bisect
import math
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence


_SMOOTH_WINDOW_MS = 500.0
_ROLLING_SHUTTER_RATIO = 0.5
_DEFAULT_RING_CAPACITY = 8192
_DEFAULT_GRAVITY_TAU_MS = 500.0
_MIN_CROP_RATIO = 0.2
_STATIC_BIAS_THRESHOLD_DEG_S = 2.0


class GyroBias(tuple):
    """Per-axis gyro bias with scalar compatibility for the Z axis."""

    __slots__ = ()

    def __new__(cls, gx=0.0, gy=0.0, gz=0.0):
        return tuple.__new__(cls, (float(gx), float(gy), float(gz)))

    @property
    def gx(self):
        return self[0]

    @property
    def gy(self):
        return self[1]

    @property
    def gz(self):
        return self[2]

    def __getitem__(self, key):
        if isinstance(key, str):
            key = key.lower()
            axes = {"gx": 0, "x": 0, "gy": 1, "y": 1, "gz": 2, "z": 2}
            if key not in axes:
                raise KeyError(key)
            key = axes[key]
        return tuple.__getitem__(self, key)

    def __float__(self):
        return self.gz

    def __eq__(self, other):
        if isinstance(other, (int, float)):
            return self.gz == other
        if isinstance(other, Mapping):
            return all(
                self[axis] == other[axis]
                for axis in ("gx", "gy", "gz")
                if axis in other
            )
        expected = getattr(other, "expected", None)
        if expected is not None:
            if isinstance(expected, (tuple, list)):
                return other == tuple(self)
            return other == self.gz
        if isinstance(other, (tuple, list)):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return NotImplemented
        return not result

    __hash__ = tuple.__hash__


def _finite_float(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _sample_axes(sample):
    if isinstance(sample, Mapping):
        gx = sample.get("gx", sample.get("wx", sample.get("x", 0.0)))
        gy = sample.get("gy", sample.get("wy", sample.get("y", 0.0)))
        gz = sample.get("gz", sample.get("wz", sample.get("z", 0.0)))
        return gx, gy, gz
    if hasattr(sample, "gx") or hasattr(sample, "gz"):
        return (
            getattr(sample, "gx", 0.0),
            getattr(sample, "gy", 0.0),
            getattr(sample, "gz", 0.0),
        )
    if isinstance(sample, Sequence) and not isinstance(sample, (str, bytes)):
        values = list(sample)
        if len(values) >= 4:
            return values[1], values[2], values[3]
        if len(values) == 3:
            return values[0], values[1], values[2]
    raise ValueError("gyro sample must contain gx, gy and gz")


def _sample_time(sample):
    if isinstance(sample, Mapping):
        for key in ("t_ms", "t", "timestamp_ms", "timestamp", "time_ms", "time"):
            if key in sample:
                return _finite_float(sample[key], "gyro timestamp")
        raise ValueError("gyro sample is missing a timestamp")
    for name in ("t_ms", "timestamp_ms", "time_ms", "timestamp", "t", "time"):
        if hasattr(sample, name):
            return _finite_float(getattr(sample, name), "gyro timestamp")
    if isinstance(sample, Sequence) and not isinstance(sample, (str, bytes)):
        values = list(sample)
        if values:
            return _finite_float(values[0], "gyro timestamp")
    raise ValueError("gyro sample is missing a timestamp")


def _sample_seq(sample):
    if isinstance(sample, Mapping):
        value = sample.get("seq", sample.get("sequence"))
    else:
        value = getattr(sample, "seq", None)
        if value is None:
            value = getattr(sample, "sequence", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sequence_wrapped(previous, current):
    if previous <= current:
        return False
    for bits in (8, 16, 32):
        size = 1 << bits
        if (
            previous >= size - max(1, size // 16)
            and current < size // 16
        ):
            return True
    return False


def _rotation_matrix(roll_deg, crop_ratio):
    angle = math.radians(roll_deg)
    scale = 1.0 / max(float(crop_ratio), _MIN_CROP_RATIO)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    a = scale * cosine
    b = -scale * sine
    d = scale * sine
    e = scale * cosine
    tx = 0.5 - 0.5 * (a + b)
    ty = 0.5 - 0.5 * (d + e)
    return [a, b, tx, d, e, ty, 0.0, 0.0, 1.0]


class GyroStabilize:
    def __init__(
        self,
        width=3840,
        height=2160,
        fps=30,
        slope_smooth=0.0,
        *,
        ring_capacity=_DEFAULT_RING_CAPACITY,
        max_samples=None,
        distortion_k1=0.0,
        distortion_k2=0.0,
    ):
        try:
            width_value = float(width)
            height_value = float(height)
        except (TypeError, ValueError) as exc:
            raise ValueError("width and height must be positive numbers") from exc
        if (
            not math.isfinite(width_value)
            or not math.isfinite(height_value)
            or width_value <= 0
            or height_value <= 0
        ):
            raise ValueError("width and height must be positive")
        fps_value = _finite_float(fps, "fps")
        if fps_value <= 0:
            raise ValueError("fps must be positive")
        smoothing = _finite_float(slope_smooth, "slope_smooth")

        if max_samples is not None:
            ring_capacity = max_samples
        capacity = int(ring_capacity)
        if capacity <= 0:
            raise ValueError("ring_capacity must be positive")

        self.width = int(width_value)
        self.height = int(height_value)
        self.fps = fps_value
        self.frame_period_ms = 1000.0 / fps_value
        self.row_readout_ms = self.frame_period_ms * _ROLLING_SHUTTER_RATIO
        self.smooth_window_ms = _SMOOTH_WINDOW_MS
        self.slope_smooth = smoothing
        self.distortion_k1 = _finite_float(distortion_k1, "distortion_k1")
        self.distortion_k2 = _finite_float(distortion_k2, "distortion_k2")

        self.ring_capacity = capacity
        self.samples = deque(maxlen=capacity)
        self.gaps = 0
        self.missing_samples = 0
        self.stale_dropped = 0
        self.overwritten = 0

        self._lock = threading.RLock()
        self._last_seq = None
        self._last_gyro_time = None
        self._last_raw_velocity = None
        self._origin_time = None
        self._cumulative_integral = [0.0, 0.0, 0.0]
        self._time_offset_ms = 0.0
        self._calibrated = False
        self._bias = GyroBias()
        self._gravity_x = 0.0
        self._gravity_y = 1.0
        self._last_plan_time = None
        self._last_plan = None
        self._samples_since_bias_update = 0

    def add_gyro(self, t_ms, gx, gy, gz, seq=None):
        t_value = _finite_float(t_ms, "gyro timestamp")
        gx_value = _finite_float(gx, "gx")
        gy_value = _finite_float(gy, "gy")
        gz_value = _finite_float(gz, "gz")
        seq_value = None if seq is None else int(seq)
        with self._lock:
            return self._add_gyro_locked(t_value, gx_value, gy_value, gz_value, seq_value)

    def _add_gyro_locked(self, t_ms, gx, gy, gz, seq):
        if seq is not None:
            if self._last_seq is not None:
                if seq <= self._last_seq:
                    if not _sequence_wrapped(self._last_seq, seq):
                        self.stale_dropped += 1
                        return False
                elif seq > self._last_seq + 1:
                    self.gaps += 1
                    self.missing_samples += seq - self._last_seq - 1
            self._last_seq = seq

        if self._last_gyro_time is not None and t_ms < self._last_gyro_time:
            self.stale_dropped += 1
            return False

        if self.samples and self.samples[-1]["t_ms"] == t_ms:
            self.stale_dropped += 1
            return False

        if len(self.samples) == self.samples.maxlen:
            self.overwritten += 1
        if self._origin_time is None:
            self._origin_time = t_ms
            cumulative = [0.0, 0.0, 0.0]
        else:
            elapsed_s = (t_ms - self._last_gyro_time) / 1000.0
            cumulative = [
                self._cumulative_integral[axis]
                + 0.5
                * (self._last_raw_velocity[axis] + (gx, gy, gz)[axis])
                * elapsed_s
                for axis in range(3)
            ]
        self.samples.append(
            {
                "t_ms": float(t_ms),
                "gx": float(gx),
                "gy": float(gy),
                "gz": float(gz),
                "seq": seq,
                "integral": tuple(cumulative),
            }
        )
        self._cumulative_integral = cumulative
        self._last_raw_velocity = (gx, gy, gz)
        self._last_gyro_time = t_ms
        self._samples_since_bias_update += 1
        if self._samples_since_bias_update >= 64:
            self._samples_since_bias_update = 0
            self._update_running_bias_locked()
        return True

    def calibrate(self, frame_times_ms, gyro_samples):
        started = time.perf_counter()
        frames = [_finite_float(value, "frame timestamp") for value in frame_times_ms]
        raw_samples = list(gyro_samples)
        parsed = []
        for sample in raw_samples:
            gx, gy, gz = _sample_axes(sample)
            parsed.append(
                {
                    "t_ms": _sample_time(sample),
                    "gx": _finite_float(gx, "gx"),
                    "gy": _finite_float(gy, "gy"),
                    "gz": _finite_float(gz, "gz"),
                    "seq": _sample_seq(sample),
                }
            )
        parsed.sort(key=lambda sample: sample["t_ms"])

        with self._lock:
            existing_keys = {
                (sample["t_ms"], sample["seq"]) for sample in self.samples
            }
            for sample in parsed:
                key = (sample["t_ms"], sample["seq"])
                if key in existing_keys:
                    continue
                if self._add_gyro_locked(
                    sample["t_ms"],
                    sample["gx"],
                    sample["gy"],
                    sample["gz"],
                    sample["seq"],
                ):
                    existing_keys.add(key)

            ordered = list(self.samples)
            gyro_times = [sample["t_ms"] for sample in ordered]
            offset_ms = 0.0
            residual_ms = 0.0
            aligned = 0
            if frames and gyro_times:
                differences = []
                for frame_time in frames:
                    index = bisect.bisect_left(gyro_times, frame_time)
                    candidates = []
                    if index < len(gyro_times):
                        candidates.append(index)
                    if index > 0:
                        candidates.append(index - 1)
                    nearest = min(
                        candidates,
                        key=lambda item: abs(frame_time - gyro_times[item]),
                    )
                    differences.append(frame_time - gyro_times[nearest])

                differences.sort()
                middle = len(differences) // 2
                if len(differences) % 2:
                    offset_ms = differences[middle]
                else:
                    offset_ms = (differences[middle - 1] + differences[middle]) / 2.0

                if len(gyro_times) > 1:
                    intervals = [
                        gyro_times[index] - gyro_times[index - 1]
                        for index in range(1, len(gyro_times))
                    ]
                    nominal_interval = sorted(intervals)[len(intervals) // 2]
                else:
                    nominal_interval = self.frame_period_ms
                tolerance = max(self.frame_period_ms, 2.0 * nominal_interval, 1.0)

                residuals = []
                aligned_times = [value + offset_ms for value in gyro_times]
                for frame_time in frames:
                    index = bisect.bisect_left(aligned_times, frame_time)
                    candidates = []
                    if index < len(aligned_times):
                        candidates.append(index)
                    if index > 0:
                        candidates.append(index - 1)
                    nearest = min(
                        candidates,
                        key=lambda item: abs(frame_time - aligned_times[item]),
                    )
                    residual = abs(frame_time - aligned_times[nearest])
                    residuals.append(residual)
                    if residual <= tolerance:
                        aligned += 1
                residual_ms = sum(residuals) / len(residuals)

            self._time_offset_ms = float(offset_ms)
            self._calibrated = True
            if parsed:
                candidate = self.estimate_bias(parsed)
                if max(abs(value) for value in candidate) <= _STATIC_BIAS_THRESHOLD_DEG_S:
                    self._bias = candidate

            result = {
                "aligned": aligned,
                "offset_ms": self._time_offset_ms,
                "residual_ms": residual_ms,
                "frames": len(frames),
                "gyro_samples": len(parsed),
                "bias": self._bias,
            }
            result["elapsed_ms"] = max(0.0, (time.perf_counter() - started) * 1000.0)
            return result

    def plan(self, frame_time_ms, shake_deg=0.0, budget_ms=8.0):
        started = time.perf_counter()
        frame_time = _finite_float(frame_time_ms, "frame_time_ms")
        shake = _finite_float(shake_deg, "shake_deg")
        budget = _finite_float(budget_ms, "budget_ms")

        with self._lock:
            rotation = self._estimate_rotation_locked(frame_time)

            if self._last_plan_time is None:
                dt_ms = self.frame_period_ms
            elif frame_time > self._last_plan_time:
                dt_ms = frame_time - self._last_plan_time
            else:
                dt_ms = 0.0

            tau_ms = (
                self.slope_smooth
                if self.slope_smooth > 0.0
                else _DEFAULT_GRAVITY_TAU_MS
            )
            alpha = 1.0 - math.exp(-max(dt_ms, 0.0) / max(tau_ms, 1e-6))
            target_angle = math.radians(
                max(-180.0, min(180.0, shake + rotation[2]))
            )
            self._gravity_x += alpha * (math.sin(target_angle) - self._gravity_x)
            self._gravity_y += alpha * (math.cos(target_angle) - self._gravity_y)
            horizon_deg = math.degrees(
                math.atan2(self._gravity_x, self._gravity_y)
            )
            if abs(horizon_deg) < 1e-12:
                horizon_deg = 0.0

            motion_deg = max(
                abs(shake),
                abs(rotation[0]),
                abs(rotation[1]),
                abs(rotation[2]),
            )
            crop_ratio = self._crop_ratio(motion_deg)
            crop_width = max(_MIN_CROP_RATIO, min(1.0, crop_ratio))
            crop_height = crop_width
            crop_x = (1.0 - crop_width) / 2.0
            crop_y = (1.0 - crop_height) / 2.0

            output_width = max(2, int((self.width * crop_width) // 2) * 2)
            output_height = max(2, int((self.height * crop_height) // 2) * 2)
            output_width = min(output_width, self.width)
            output_height = min(output_height, self.height)

            estimated_cost = self._estimated_full_cost_ms()
            precision = "reduced" if budget < estimated_cost else "full"
            row_fractions = (
                (0.0, 0.5, 1.0)
                if precision == "reduced"
                else (0.0, 0.25, 0.5, 0.75, 1.0)
            )
            row_rotations = []
            row_matrices = []
            for fraction in row_fractions:
                row_time = frame_time + fraction * self.row_readout_ms
                row_rotation = self._estimate_rotation_locked(row_time)
                row_rotations.append(list(row_rotation))
                row_matrices.append(
                    _rotation_matrix(row_rotation[2] + horizon_deg, crop_width)
                )
            center_index = len(row_matrices) // 2
            matrix = list(row_matrices[center_index])

            distortion_matrix = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
            result = {
                "crop_ratio": crop_ratio,
                "crop_rect": [crop_x, crop_y, crop_width, crop_height],
                "output_size": (output_width, output_height),
                "horizon_deg": horizon_deg,
                "smooth_window_ms": self.smooth_window_ms,
                "row_readout_ms": self.row_readout_ms,
                "row_times_ms": [
                    frame_time + fraction * self.row_readout_ms
                    for fraction in row_fractions
                ],
                "row_rotations_deg": row_rotations,
                "row_matrices": row_matrices,
                "matrix": matrix,
                "distortion_matrix": distortion_matrix,
                "stage_order": ["distortion", "stabilize"],
                "precision": precision,
                "estimated_cost_ms": estimated_cost,
                "budget_ms": budget,
                "rotation_deg": list(rotation),
                "frame_time_ms": frame_time,
            }
            result["elapsed_ms"] = max(
                0.0, (time.perf_counter() - started) * 1000.0
            )
            self._last_plan_time = frame_time
            self._last_plan = result
            return result

    def estimate_rotation(self, t_ms):
        t_value = _finite_float(t_ms, "timestamp")
        with self._lock:
            return self._estimate_rotation_locked(t_value)

    def _estimate_rotation_locked(self, t_ms):
        raw_time = t_ms - self._time_offset_ms
        samples = list(self.samples)
        if not samples or self._origin_time is None:
            return (0.0, 0.0, 0.0)

        times = [sample["t_ms"] for sample in samples]
        if raw_time <= self._origin_time + 1e-12:
            return (0.0, 0.0, 0.0)

        if raw_time < times[0]:
            delta_s = (raw_time - times[0]) / 1000.0
            raw_integral = [
                samples[0]["integral"][axis]
                + (samples[0]["gx"], samples[0]["gy"], samples[0]["gz"])[axis]
                * delta_s
                for axis in range(3)
            ]
        elif raw_time <= times[-1]:
            index = bisect.bisect_left(times, raw_time)
            if index < len(times) and times[index] == raw_time:
                raw_integral = list(samples[index]["integral"])
            else:
                previous = samples[index - 1]
                current = samples[index]
                duration_s = (raw_time - previous["t_ms"]) / 1000.0
                fraction = (
                    (raw_time - previous["t_ms"])
                    / (current["t_ms"] - previous["t_ms"])
                )
                previous_raw = (
                    previous["gx"],
                    previous["gy"],
                    previous["gz"],
                )
                current_raw = (
                    current["gx"],
                    current["gy"],
                    current["gz"],
                )
                raw_integral = [
                    previous["integral"][axis]
                    + 0.5
                    * (
                        previous_raw[axis]
                        + previous_raw[axis]
                        + fraction * (current_raw[axis] - previous_raw[axis])
                    )
                    * duration_s
                    for axis in range(3)
                ]
        else:
            last = samples[-1]
            last_raw = (last["gx"], last["gy"], last["gz"])
            gap_s = (raw_time - times[-1]) / 1000.0
            raw_integral = [
                last["integral"][axis]
                + last_raw[axis] * gap_s
                for axis in range(3)
            ]

        elapsed_s = (raw_time - self._origin_time) / 1000.0
        return tuple(
            raw_integral[axis] - self._bias[axis] * elapsed_s
            for axis in range(3)
        )

    def _sample_velocity(self, sample):
        return (
            sample["gx"] - self._bias[0],
            sample["gy"] - self._bias[1],
            sample["gz"] - self._bias[2],
        )

    def _interpolate_velocity(self, first, second, fraction):
        first_velocity = self._sample_velocity(first)
        second_velocity = self._sample_velocity(second)
        return tuple(
            first_velocity[axis]
            + fraction * (second_velocity[axis] - first_velocity[axis])
            for axis in range(3)
        )

    def estimate_bias(self, samples):
        parsed = []
        for sample in samples:
            gx, gy, gz = _sample_axes(sample)
            parsed.append(
                (
                    _finite_float(gx, "gx"),
                    _finite_float(gy, "gy"),
                    _finite_float(gz, "gz"),
                )
            )
        return self._average_bias(parsed)

    @staticmethod
    def _average_bias(samples):
        if not samples:
            return GyroBias()
        count = float(len(samples))
        return GyroBias(
            sum(sample[0] for sample in samples) / count,
            sum(sample[1] for sample in samples) / count,
            sum(sample[2] for sample in samples) / count,
        )

    def _update_running_bias_locked(self):
        window = list(self.samples)[-64:]
        static = [
            (sample["gx"], sample["gy"], sample["gz"])
            for sample in window
            if max(abs(sample["gx"]), abs(sample["gy"]), abs(sample["gz"]))
            <= _STATIC_BIAS_THRESHOLD_DEG_S
        ]
        if len(static) >= 32:
            self._bias = self._average_bias(static)

    def _crop_ratio(self, shake_deg):
        angle_deg = min(max(abs(shake_deg), 0.0), 45.0)
        angle = math.radians(angle_deg)
        cosine = abs(math.cos(angle))
        sine = abs(math.sin(angle))
        aspect = float(self.width) / float(self.height)
        safe_for_width = 1.0 / (cosine + sine / aspect)
        safe_for_height = 1.0 / (aspect * sine + cosine)
        rotation_safe = min(safe_for_width, safe_for_height)
        translation_safe = 1.0 / (1.0 + 2.0 * abs(math.tan(angle)))
        ratio = min(rotation_safe, translation_safe, 1.0)
        return max(_MIN_CROP_RATIO, min(1.0, ratio))

    def _estimated_full_cost_ms(self):
        pixels = float(self.width) * float(self.height)
        row_weight = 0.8 if self.height >= 2000 else 0.35
        return min(15.0, 0.8 + pixels / 600000.0 + row_weight)

    def stats(self):
        with self._lock:
            samples = list(self.samples)
            return {
                "samples": len(samples),
                "gaps": self.gaps,
                "missing_samples": self.missing_samples,
                "stale_dropped": self.stale_dropped,
                "overwritten": self.overwritten,
                "capacity": self.ring_capacity,
                "latest_seq": self._last_seq,
                "bias": self._bias,
                "time_offset_ms": self._time_offset_ms,
            }
