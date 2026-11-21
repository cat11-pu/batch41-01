"""WSOLA time stretching with an explicit planning step."""

from collections import deque
from fractions import Fraction
import math
import threading
import time


class TimeStretchWsola:
    """Offline and streaming WSOLA time-stretch engine.

    ``plan`` is deliberately side-effect free. It validates the request,
    derives all timing constants once, and returns the exact output length.
    ``process`` consumes that plan so the duration cannot drift while audio is
    being generated.
    """

    def __init__(self, sample_rate=48000, channels=2, window_ms=20):
        self.sample_rate = float(sample_rate)
        self.channels = int(channels)
        self.window_ms = float(window_ms)

        if not math.isfinite(self.sample_rate) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if not math.isfinite(self.window_ms) or self.window_ms <= 0:
            raise ValueError("window_ms must be positive")

        self._base_window = max(1, int(round(self.sample_rate * self.window_ms / 1000.0)))
        self._hop_out = max(1, self._base_window // 2)
        self._search_radius = max(1, int(round(self.sample_rate * 0.003)))

        self._ring = deque()
        self._ring_lock = threading.Lock()
        self._read_pos = 0
        self._write_pos = 0

        self._phase = 0
        self._seek_position = 0
        self._preroll_pending = 0
        self._last_plan = None

    def plan(self, samples, rate_num, rate_den=1, resample_to=None):
        """Validate the request and derive an exact time-stretch plan."""
        num, den = self._rate_parts(rate_num, rate_den)
        target_rate = self._resample_parts(resample_to)

        data = self._as_float_list(samples)
        input_samples = len(data)
        if input_samples == 0:
            raise ValueError("input must not be empty")

        hop_in = max(1, int(round(self._hop_out * num / den)))
        analysis_window = max(self._base_window, hop_in + self._hop_out)
        if input_samples < analysis_window:
            raise ValueError(
                "input is shorter than one analysis window "
                f"({input_samples} < {analysis_window})"
            )

        stretch_samples = (input_samples * den) // num
        if stretch_samples <= 0:
            raise ValueError("rate produces no output samples")

        transients = self._detect_transients_sequence(data)
        silence_ratio = self._silence_ratio(data, analysis_window)
        overlap_strength = 0.5 + 0.5 * (1.0 - silence_ratio)

        if target_rate is None:
            output_samples = stretch_samples
            stage_order = ["stretch"]
        else:
            target_num, target_den = target_rate
            source_num, source_den = self._rate_parts(int(round(self.sample_rate)), 1)
            output_samples = (
                stretch_samples * target_num * source_den
            ) // (target_den * source_num)
            stage_order = ["stretch", "resample"]

        if output_samples <= 0:
            raise ValueError("requested output length is zero")

        latency = max(0, analysis_window - self._hop_out)
        result = {
            "sample_rate": int(round(self.sample_rate)),
            "channels": self.channels,
            "rate_num": num,
            "rate_den": den,
            "rate": num / den,
            "input_samples": input_samples,
            "window": self._base_window,
            "window_ms": self.window_ms,
            "hop_out": self._hop_out,
            "hop_in": hop_in,
            "analysis_window": analysis_window,
            "overlap": analysis_window - self._hop_out,
            "search_radius": min(self._search_radius, max(1, self._hop_out // 2)),
            "stretch_samples": stretch_samples,
            "output_samples": output_samples,
            "stage_order": stage_order,
            "pitch_compensation": target_rate is not None,
            "resample_to": (
                None
                if target_rate is None
                else target_rate[0] / target_rate[1]
            ),
            "transient_positions": transients,
            "transient_count": len(transients),
            "preserve_transients": True,
            "silence_ratio": float(silence_ratio),
            "overlap_strength": float(overlap_strength),
            "latency_samples": int(latency),
            "delay_samples": int(latency),
        }
        self._last_plan = result
        self._phase = 0
        self._preroll_pending = latency
        return result

    def process(self, samples, rate_num, rate_den=1, plan=None, resample_to=None):
        """Render stretched audio, optionally followed by sample-rate conversion."""
        started = time.perf_counter()
        num, den = self._rate_parts(rate_num, rate_den)

        if plan is None:
            plan = self.plan(samples, num, den, resample_to=resample_to)
        else:
            data_length = len(self._as_float_list(samples))
            required = int(plan.get("analysis_window", self._base_window))
            if data_length < required:
                raise ValueError("input is shorter than the planned analysis window")

        data = self._as_float_list(samples)
        stretched = self._wsola(data, plan)

        if plan["stage_order"] == ["stretch", "resample"]:
            resample_plan = self._resample_parts(
                plan.get("resample_to") if resample_to is None else resample_to
            )
            if resample_plan is None:
                raise ValueError("plan requires a resample target")
            audio = self._resample(stretched, resample_plan, plan["output_samples"])
        else:
            audio = stretched

        expected = int(plan["output_samples"])
        if len(audio) > expected:
            del audio[expected:]
        elif len(audio) < expected:
            audio.extend([0.0] * (expected - len(audio)))

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        channels = max(1, int(plan.get("channels", self.channels)))
        offsets = [int(plan.get("_last_offset", 0))] * channels
        result = {
            "audio": audio,
            "output_samples": expected,
            "rate": num / den,
            "latency_samples": int(plan["latency_samples"]),
            "elapsed_ms": float(elapsed_ms),
            "stage_order": list(plan["stage_order"]),
            "pitch_compensation": bool(plan["pitch_compensation"]),
            "transient_count": int(plan["transient_count"]),
            "silence_ratio": float(plan["silence_ratio"]),
            "overlap_strength": float(plan["overlap_strength"]),
            "channel_offsets": offsets,
        }
        self._last_plan = dict(plan)
        self._last_plan["elapsed_ms"] = elapsed_ms
        return result

    def detect_transients(self, samples):
        """Return sample indices of sharp energy onsets."""
        return self._detect_transients_sequence(self._as_float_list(samples))

    def seek(self, sample_index):
        """Reset WSOLA phase and schedule a pre-roll at a new position."""
        if isinstance(sample_index, bool) or not isinstance(sample_index, int):
            raise ValueError("sample_index must be an integer")
        if sample_index < 0:
            raise ValueError("sample_index must not be negative")

        preroll = max(1, self._base_window - self._hop_out)
        with self._ring_lock:
            self._ring.clear()
            self._read_pos = 0
            self._write_pos = 0
        self._phase = 0
        self._seek_position = sample_index
        self._preroll_pending = preroll
        return {
            "position": sample_index,
            "phase": self._phase,
            "preroll_samples": preroll,
            "reset": True,
        }

    def push(self, samples):
        """Append decoded samples to the streaming ring buffer."""
        data = self._as_float_list(samples)
        with self._ring_lock:
            self._ring.extend(data)
            self._write_pos += len(data)
            buffered = len(self._ring)
        return buffered

    def pull(self, count):
        """Remove up to ``count`` samples from the streaming ring buffer."""
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("count must be an integer")
        if count < 0:
            raise ValueError("count must not be negative")

        with self._ring_lock:
            available = min(count, len(self._ring))
            result = [self._ring.popleft() for _ in range(available)]
            self._read_pos += available
        if len(result) < count:
            result.extend([0.0] * (count - len(result)))
        return result

    def stats(self):
        """Return an atomic snapshot of streaming and processing state."""
        with self._ring_lock:
            read_pos = self._read_pos
            write_pos = self._write_pos
            buffered = len(self._ring)
        plan = self._last_plan or {}
        return {
            "read_pos": read_pos,
            "write_pos": write_pos,
            "buffered": buffered,
            "rate": plan.get("rate"),
            "latency_samples": int(plan.get("latency_samples", 0)),
            "elapsed_ms": float(plan.get("elapsed_ms", 0.0)),
        }

    def _wsola(self, samples, plan):
        channels = max(1, int(plan.get("channels", self.channels)))
        total = len(samples)
        frames = max(1, (total + channels - 1) // channels)
        analysis_window = int(plan["analysis_window"])
        hop_out = int(plan["hop_out"])
        radius = int(plan.get("search_radius", self._search_radius))
        overlap = max(1, analysis_window - hop_out)
        stretch_samples = int(plan["stretch_samples"])
        target_frames = stretch_samples // channels
        remainder = stretch_samples - target_frames * channels

        channel_data = []
        for channel in range(channels):
            channel_data.append(
                [
                    float(samples[index * channels + channel])
                    if index * channels + channel < total
                    else 0.0
                    for index in range(frames)
                ]
            )
        mono = [
            sum(channel_data[channel][index] for channel in range(channels)) / channels
            for index in range(frames)
        ]

        transient_frames = {
            index // channels
            for index in self._detect_transients_sequence(samples)
        }
        peak = max((abs(value) for value in mono), default=0.0)
        silence_floor = max(1e-4, peak * 0.01)
        window = self._analysis_window(analysis_window)

        output_length = target_frames + analysis_window + hop_out
        out = [[0.0] * output_length for _ in range(channels)]
        norm = [0.0] * output_length
        frame_index = 0
        numerator = 0
        denominator = int(plan["rate_den"])
        rate_num = int(plan["rate_num"])
        previous_in = None
        last_offset = 0
        rendered = 0

        while rendered < target_frames:
            base_in = numerator // denominator
            out_pos = frame_index * hop_out
            if out_pos >= target_frames and frame_index > 0:
                break

            local = mono[base_in : base_in + analysis_window]
            local_silent = self._is_silent(local, silence_floor)
            is_transient = any(
                base_in <= index < base_in + analysis_window
                for index in transient_frames
            )

            offset = 0
            if previous_in is not None and not local_silent and not is_transient:
                offset = self._best_offset(
                    mono,
                    previous_in,
                    base_in,
                    hop_out,
                    overlap,
                    radius,
                    frames,
                )
            if is_transient:
                previous_in = None
            last_offset = offset

            source = base_in + offset
            for tap in range(analysis_window):
                output_index = out_pos + tap
                if output_index >= target_frames:
                    break
                weight = window[tap]
                norm[output_index] += weight
                source_index = source + tap
                if 0 <= source_index < frames:
                    for channel in range(channels):
                        out[channel][output_index] += (
                            channel_data[channel][source_index] * weight
                        )

            if not local_silent and not is_transient:
                previous_in = source
            rendered = min(target_frames, out_pos + analysis_window)
            frame_index += 1
            numerator += hop_out * rate_num
            if base_in > frames + analysis_window:
                break

        audio = [0.0] * (target_frames * channels + remainder)
        for index in range(target_frames):
            weight = norm[index]
            for channel in range(channels):
                value = out[channel][index] / weight if weight > 1e-12 else 0.0
                audio[index * channels + channel] = value

        plan["_last_offset"] = last_offset
        return audio

    def _best_offset(
        self,
        mono,
        previous_in,
        base_in,
        hop_out,
        overlap,
        radius,
        frames,
    ):
        reference_start = previous_in + hop_out
        if reference_start < 0 or reference_start + overlap > len(mono):
            return 0
        reference = mono[reference_start : reference_start + overlap]
        reference_energy = sum(value * value for value in reference)
        if reference_energy <= 1e-12:
            return 0

        best_offset = 0
        best_score = -math.inf
        lower = max(-radius, -base_in)
        upper = min(radius, frames - overlap - base_in)
        if upper < lower:
            return 0
        for offset in range(lower, upper + 1):
            start = base_in + offset
            candidate = mono[start : start + overlap]
            candidate_energy = sum(value * value for value in candidate)
            if candidate_energy <= 1e-12:
                continue
            dot = sum(a * b for a, b in zip(reference, candidate))
            score = dot / math.sqrt(reference_energy * candidate_energy)
            score -= 1e-4 * abs(offset) / max(1, radius)
            if score > best_score:
                best_score = score
                best_offset = offset
        return best_offset

    def _resample(self, samples, target_rate, expected):
        source_num, source_den = self._rate_parts(int(round(self.sample_rate)), 1)
        target_num, target_den = target_rate
        if len(samples) == 0 or expected <= 0:
            return [0.0] * max(0, expected)

        step = Fraction(source_num * target_den, source_den * target_num)
        result = []
        last = len(samples) - 1
        for index in range(expected):
            position = index * step
            left = int(position)
            fraction = float(position - left)
            if left >= last:
                result.append(float(samples[last]))
            else:
                result.append(
                    float(samples[left]) * (1.0 - fraction)
                    + float(samples[left + 1]) * fraction
                )
        return result

    def _detect_transients_sequence(self, data):
        length = len(data)
        if length == 0:
            return []
        peak = max(abs(value) for value in data)
        if peak <= 1e-12:
            return []

        frame = max(8, int(round(self.sample_rate * 0.005)))
        hop = max(1, frame // 2)
        energies = []
        peaks = []
        starts = []
        for start in range(0, length, hop):
            segment = data[start : start + frame]
            if not segment:
                break
            energy = sum(value * value for value in segment) / len(segment)
            energies.append(energy)
            peaks.append(max(abs(value) for value in segment))
            starts.append(start)

        candidates = []
        absolute_floor = max(1e-8, peak * 1e-4)
        for index, energy in enumerate(energies):
            previous = max(
                energies[max(0, index - 3) : index],
                default=0.0,
            )
            rise = energy > max(absolute_floor, previous * 4.0)
            prominent = peaks[index] >= max(1e-4, peak * 0.05)
            if not rise or not prominent:
                continue
            segment = data[starts[index] : starts[index] + frame]
            local_peak = max(range(len(segment)), key=lambda item: abs(segment[item]))
            candidates.append((starts[index] + local_peak, abs(segment[local_peak])))

        candidates.sort()
        selected = []
        suppression = max(1, frame // 2)
        for position, strength in candidates:
            if selected and position - selected[-1][0] < suppression:
                if strength > selected[-1][1]:
                    selected[-1] = (position, strength)
                continue
            selected.append((position, strength))
        return [position for position, _ in selected]

    def _is_silent(self, segment, floor):
        if not segment:
            return True
        mean_square = sum(value * value for value in segment) / len(segment)
        return math.sqrt(mean_square) <= floor

    def _silence_ratio(self, data, analysis_window):
        length = len(data)
        if length == 0:
            return 1.0
        peak = max(abs(value) for value in data)
        floor = max(1e-4, peak * 0.01)
        frame = max(1, analysis_window // 2)
        total = 0
        silent = 0
        for start in range(0, length, frame):
            segment = data[start : start + frame]
            total += 1
            if self._is_silent(segment, floor):
                silent += 1
        return silent / total if total else 1.0

    def _analysis_window(self, length):
        return [
            0.5 - 0.5 * math.cos(2.0 * math.pi * (index + 0.5) / length)
            for index in range(length)
        ]

    def _rate_parts(self, rate_num, rate_den):
        if isinstance(rate_num, bool) or isinstance(rate_den, bool):
            raise ValueError("rate must be a positive rational number")
        try:
            if isinstance(rate_num, float):
                if not math.isfinite(rate_num):
                    raise ValueError("rate must be finite")
                fraction = Fraction(rate_num).limit_denominator(1_000_000)
                if isinstance(rate_den, (int, float)) and rate_den != 1:
                    if not math.isfinite(float(rate_den)):
                        raise ValueError("rate denominator must be finite")
                    fraction /= Fraction(rate_den).limit_denominator(1_000_000)
            else:
                numerator = int(rate_num)
                denominator = int(rate_den)
                fraction = Fraction(numerator, denominator)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ValueError("rate must be a positive rational number") from exc

        if fraction <= 0:
            raise ValueError("rate must be greater than zero")
        return fraction.numerator, fraction.denominator

    def _resample_parts(self, resample_to):
        if resample_to is None:
            return None
        if isinstance(resample_to, bool):
            raise ValueError("resample_to must be positive")
        try:
            rate = float(resample_to)
        except (TypeError, ValueError) as exc:
            raise ValueError("resample_to must be positive") from exc
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("resample_to must be positive")
        if int(round(rate)) == int(round(self.sample_rate)):
            return None
        fraction = Fraction(rate).limit_denominator(1_000_000)
        return fraction.numerator, fraction.denominator

    @staticmethod
    def _as_float_list(samples):
        if samples is None:
            raise ValueError("samples must be a sequence")
        if isinstance(samples, (str, bytes, bytearray)):
            raise ValueError("samples must be numeric")
        if hasattr(samples, "tolist") and not isinstance(samples, (list, tuple)):
            samples = samples.tolist()
        try:
            values = list(samples)
        except TypeError as exc:
            raise ValueError("samples must be a sequence") from exc
        result = []
        for value in values:
            if isinstance(value, (list, tuple)):
                raise ValueError("samples must be a flat numeric sequence")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("samples must be numeric") from exc
            if not math.isfinite(number):
                raise ValueError("samples must be finite")
            result.append(number)
        return result
