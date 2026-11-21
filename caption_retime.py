"""Retiming of subtitle captions between rational frame rates.

Frame rates are always handled as exact rational numbers. Every caption is
mapped from its absolute position on the source timeline, so rounding never
accumulates across captions. Conversion and validation run as two separate
passes: inputs are validated first, then each caption is mapped, clipped and
normalised.
"""

import time
from bisect import bisect_right
from fractions import Fraction


def _rational(value):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, (tuple, list)):
        return Fraction(value[0], value[1])
    return Fraction(value)


def _scale_frame(frame, numerator, denominator):
    """Round frame * numerator / denominator to the nearest integer, half up."""
    quotient, remainder = divmod(frame * numerator, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient


def _round_half_up(value):
    quotient, remainder = divmod(value.numerator, value.denominator)
    if remainder * 2 >= value.denominator:
        quotient += 1
    return quotient


class CaptionRetime:
    def __init__(self, min_duration_frames=2):
        self.min_duration_frames = min_duration_frames

    def retime(self, captions, source_fps=(24000, 1001), target_fps=(25, 1),
               video_frames=None, offset_frames=0):
        self._validate(captions)
        started_at = time.perf_counter()

        source_rate = _rational(source_fps)
        target_rate = _rational(target_fps)
        numerator = target_rate.numerator * source_rate.denominator
        denominator = target_rate.denominator * source_rate.numerator

        def map_frame(frame):
            return _scale_frame(frame, numerator, denominator) + offset_frames

        mapped, clipped = self._map_captions(captions, map_frame, video_frames)
        result_captions = self._normalize(mapped, video_frames)

        return {
            "captions": result_captions,
            "count": len(captions),
            "clipped": clipped,
            "offset": offset_frames,
            "elapsed_ms": (time.perf_counter() - started_at) * 1000.0,
        }

    def retime_vfr(self, captions, segments, target_fps=(25, 1), video_frames=None):
        self._validate(captions)
        started_at = time.perf_counter()

        target_rate = _rational(target_fps)
        bounds = [segment["start"] for segment in segments]
        rates = [_rational(segment["fps"]) for segment in segments]

        # Target time already elapsed when each segment begins, kept exact.
        boundaries = [Fraction(0)]
        for index in range(1, len(segments)):
            segment_length = bounds[index] - bounds[index - 1]
            boundaries.append(
                boundaries[-1]
                + segment_length * target_rate / rates[index - 1]
            )

        def map_frame(frame):
            index = bisect_right(bounds, frame) - 1
            local_frame = frame - bounds[index]
            exact = boundaries[index] + local_frame * target_rate / rates[index]
            return _round_half_up(exact)

        mapped, clipped = self._map_captions(captions, map_frame, video_frames)
        result_captions = self._normalize(mapped, video_frames)

        return {
            "captions": result_captions,
            "count": len(captions),
            "clipped": clipped,
            "offset": 0,
            "elapsed_ms": (time.perf_counter() - started_at) * 1000.0,
        }

    def timecode_to_frame(self, timecode, fps=(30000, 1001), drop_frame=False):
        hours, minutes, seconds, frames = (
            int(part) for part in timecode.replace(";", ":").split(":")
        )
        nominal_fps = round(_rational(fps))
        nominal = ((hours * 60 + minutes) * 60 + seconds) * nominal_fps + frames
        if not drop_frame:
            return nominal

        drop_increment = nominal_fps // 15
        total_minutes = hours * 60 + minutes
        dropped = drop_increment * (total_minutes - total_minutes // 10)
        return nominal - dropped

    def frame_to_timecode(self, frame, fps=(30000, 1001), drop_frame=False):
        nominal_fps = round(_rational(fps))
        if drop_frame:
            drop_increment = nominal_fps // 15
            minutes = frame // (60 * nominal_fps - drop_increment)
            nominal = frame + drop_increment * (minutes - minutes // 10)
        else:
            nominal = frame

        hours, remainder = divmod(nominal, 3600 * nominal_fps)
        minutes, remainder = divmod(remainder, 60 * nominal_fps)
        seconds, frames = divmod(remainder, nominal_fps)
        separator = ";" if drop_frame else ":"
        return (
            f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{frames:02d}"
        )

    def batch(self, projects):
        results = {}
        for project in projects:
            results[project["name"]] = self.retime(
                project["captions"],
                source_fps=project["source_fps"],
                target_fps=project["target_fps"],
                video_frames=project.get("video_frames"),
                offset_frames=project.get("offset_frames", 0),
            )
        return results

    def _validate(self, captions):
        for caption in captions:
            if caption["start"] < 0:
                raise ValueError(
                    f"caption {caption.get('id')!r} has negative start"
                )
            if caption["duration"] <= 0:
                raise ValueError(
                    f"caption {caption.get('id')!r} has non-positive duration"
                )

    def _map_captions(self, captions, map_frame, video_frames):
        """First scan: map absolute source frames onto the target timeline."""
        mapped = []
        clipped = 0
        for caption in captions:
            start = map_frame(caption["start"])
            end = map_frame(caption["start"] + caption["duration"])
            entry = {
                "raw": caption,
                "start": start,
                "end": end,
                "clipped": False,
            }
            if video_frames is not None and end > video_frames:
                clipped += 1
                entry["clipped"] = True
                entry["end"] = video_frames
            mapped.append(entry)
        return mapped, clipped

    def _normalize(self, mapped, video_frames):
        """Second scan: clip, enforce minimum duration and keep order."""
        results = []
        for index, entry in enumerate(mapped):
            start = entry["start"]
            end = entry["end"]
            if video_frames is not None and start >= video_frames:
                continue

            duration = end - start
            if not entry["clipped"] and duration < self.min_duration_frames:
                duration = self.min_duration_frames
                if index + 1 < len(mapped):
                    next_start = mapped[index + 1]["start"]
                    duration = min(duration, max(0, next_start - start))

            caption = dict(entry["raw"])
            caption["start"] = start
            caption["duration"] = duration
            results.append(caption)
        return results
