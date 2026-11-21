"""Frame accurate timeline snapping, moving, rippling and undo."""

from __future__ import annotations

import copy
import math
import threading
import time
from decimal import Decimal
from fractions import Fraction


class TimelineSnapEngine:
    def __init__(self, fps_num=30, fps_den=1):
        if fps_num <= 0 or fps_den <= 0:
            raise ValueError("fps must be positive")
        self.fps_num = int(fps_num)
        self.fps_den = int(fps_den)
        self.tracks = {}
        self.clips = {}
        self.groups = {}
        self.history = []
        self._edit_owner = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Frame arithmetic
    # ------------------------------------------------------------------
    @staticmethod
    def _fraction(value):
        if isinstance(value, Fraction):
            return value
        if isinstance(value, bool):
            return Fraction(int(value))
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("time must be finite")
            # str() keeps decimal input exact instead of inheriting binary
            # floating point noise (e.g. 10.1 becoming 10.099999...).
            return Fraction(str(value))
        if isinstance(value, Decimal):
            return Fraction(value)
        return Fraction(value)

    @classmethod
    def _round_half_up(cls, value):
        value = cls._fraction(value)
        sign = -1 if value < 0 else 1
        value = abs(value)
        whole, remainder = divmod(value.numerator, value.denominator)
        if remainder * 2 >= value.denominator:
            whole += 1
        return sign * whole

    @classmethod
    def _frame(cls, value):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return cls._round_half_up(value)

    def _require_clip(self, clip_id):
        try:
            return self.clips[clip_id]
        except KeyError:
            raise KeyError("unknown clip: %r" % (clip_id,)) from None

    def _require_track(self, track_id):
        try:
            return self.tracks[track_id]
        except KeyError:
            raise KeyError("unknown track: %r" % (track_id,)) from None

    @staticmethod
    def _links(track):
        linked = track.get("linked")
        if linked is None:
            return ()
        if isinstance(linked, str):
            return (linked,)
        try:
            return tuple(linked)
        except TypeError:
            return (linked,)

    def _linked_tracks(self, track_id):
        """Return the transitive, non-locked tracks linked to ``track_id``."""
        seen = set()
        pending = [track_id]
        while pending:
            current = pending.pop()
            for other_id, other in self.tracks.items():
                if other_id == track_id or other_id in seen:
                    continue
                if other_id in self._links(self.tracks[current]) or current in self._links(other):
                    seen.add(other_id)
                    pending.append(other_id)
        return {track_id for track_id in seen if not self.tracks[track_id].get("locked")}

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def add_track(self, track_id, kind="video", locked=False, linked=None):
        with self._lock:
            self.tracks[track_id] = {
                "id": track_id,
                "kind": kind,
                "locked": bool(locked),
                "linked": copy.deepcopy(linked),
            }

    def add_clip(self, clip_id, track_id, start, duration, transition=0):
        with self._lock:
            self._require_track(track_id)
            if self._fraction(start) < 0:
                raise ValueError("clip start cannot be negative")
            start_frame = self._frame(start)
            duration_frame = self._frame(duration)
            transition_frame = self._frame(transition)
            if start_frame < 0:
                raise ValueError("clip start cannot be negative")
            if duration_frame <= 0:
                raise ValueError("clip duration must be positive")
            if transition_frame < 0:
                raise ValueError("transition cannot be negative")
            if clip_id in self.clips:
                raise ValueError("duplicate clip: %r" % (clip_id,))
            self.clips[clip_id] = {
                "id": clip_id,
                "track": track_id,
                "start": start_frame,
                "duration": duration_frame,
                "transition": transition_frame,
            }

    def clip_state(self, clip_id):
        with self._lock:
            clip = self._require_clip(clip_id)
            state = dict(clip)
            state["end"] = clip["start"] + clip["duration"]
            return state

    def add_group(self, group_id, clip_ids):
        with self._lock:
            ids = []
            for clip_id in clip_ids:
                self._require_clip(clip_id)
                if clip_id not in ids:
                    ids.append(clip_id)
            self.groups[group_id] = {"id": group_id, "clips": ids, "collapsed": False}

    def collapse_group(self, group_id, collapsed=True):
        with self._lock:
            try:
                group = self.groups[group_id]
            except KeyError:
                raise KeyError("unknown group: %r" % (group_id,)) from None
            group["collapsed"] = bool(collapsed)

    # ------------------------------------------------------------------
    # Snapping
    # ------------------------------------------------------------------
    def snap(self, track_id, frame, tolerance=2, exclude=None):
        started = time.perf_counter()
        with self._lock:
            self._require_track(track_id)
            timeline_frame = self._frame(frame)
            tolerance = self._fraction(tolerance)
            if tolerance < 0:
                raise ValueError("tolerance cannot be negative")

            if exclude is None:
                excluded = set()
            elif isinstance(exclude, (str, bytes)) or not hasattr(exclude, "__iter__"):
                excluded = {exclude}
            else:
                excluded = set(exclude)

            nearest = None
            nearest_distance = None
            for clip_id, clip in self.clips.items():
                if clip_id in excluded:
                    continue
                track = self.tracks.get(clip["track"])
                if track is None or track.get("locked"):
                    continue
                for edge in (clip["start"], clip["start"] + clip["duration"]):
                    edge = self._frame(edge)
                    distance = abs(timeline_frame - edge)
                    if distance > tolerance:
                        continue
                    if (
                        nearest is None
                        or distance < nearest_distance
                        or (distance == nearest_distance and edge < nearest)
                    ):
                        nearest = edge
                        nearest_distance = distance

            resolved_frame = nearest if nearest is not None else timeline_frame
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return {
                "point": nearest,
                "frame": resolved_frame,
                "snapped": nearest is not None,
                "elapsed_ms": elapsed_ms,
            }

    # ------------------------------------------------------------------
    # Editing / serialization
    # ------------------------------------------------------------------
    def _claim(self, owner):
        if self._edit_owner is not None and owner != self._edit_owner:
            raise RuntimeError(
                "timeline edit already owned by %r" % (self._edit_owner,)
            )

    def begin_edit(self, owner):
        with self._lock:
            if self._edit_owner is not None:
                raise RuntimeError(
                    "timeline edit already owned by %r" % (self._edit_owner,)
                )
            self._edit_owner = owner

    def end_edit(self, owner):
        with self._lock:
            if self._edit_owner is None:
                raise RuntimeError("no timeline edit is active")
            if owner != self._edit_owner:
                raise RuntimeError(
                    "timeline edit owned by %r, not %r"
                    % (self._edit_owner, owner)
                )
            self._edit_owner = None

    def snapshot(self):
        with self._lock:
            return {
                "fps_num": self.fps_num,
                "fps_den": self.fps_den,
                "tracks": copy.deepcopy(self.tracks),
                "clips": copy.deepcopy(self.clips),
                "groups": copy.deepcopy(self.groups),
            }

    def _restore(self, state):
        self.tracks = copy.deepcopy(state["tracks"])
        self.clips = copy.deepcopy(state["clips"])
        self.groups = copy.deepcopy(state["groups"])

    def _record(self, before):
        after = self.snapshot()
        if before != after:
            self.history.append({"before": before, "after": after})

    def _validate_changes(self, changes):
        for clip_id, new_start in changes.items():
            clip = self._require_clip(clip_id)
            track = self._require_track(clip["track"])
            if track.get("locked"):
                raise ValueError("track %r is locked" % (clip["track"],))
            if new_start < 0:
                raise ValueError("clip start cannot be negative")

    def _apply_changes(self, changes):
        self._validate_changes(changes)
        for clip_id, new_start in changes.items():
            self.clips[clip_id]["start"] = new_start

    def _primary_changes(self, clip_ids, target):
        clips = [self._require_clip(clip_id) for clip_id in clip_ids]
        for clip in clips:
            track = self._require_track(clip["track"])
            if track.get("locked"):
                raise ValueError("track %r is locked" % (clip["track"],))

        if self._fraction(target) < 0:
            raise ValueError("clip start cannot be negative")
        target = self._frame(target)
        first_start = min(clip["start"] for clip in clips)
        delta = target - first_start

        changes = {}
        for clip in clips:
            changes[clip["id"]] = clip["start"] + delta

        # Linked tracks are part of the same magnetic selection.  Only clips
        # that actually live inside the moved clip are carried along; an
        # unrelated subtitle elsewhere on the linked track keeps its timing.
        for clip in clips:
            linked_tracks = self._linked_tracks(clip["track"])
            if not linked_tracks:
                continue
            clip_end = clip["start"] + clip["duration"]
            for companion_id, companion in self.clips.items():
                if companion_id in changes or companion_id in clip_ids:
                    continue
                if companion["track"] not in linked_tracks:
                    continue
                if clip["start"] <= companion["start"] < clip_end:
                    changes[companion_id] = companion["start"] + delta
        return changes

    def _move(self, clip_ids, target, owner):
        self._claim(owner)
        changes = self._primary_changes(clip_ids, target)
        if not changes:
            return
        before = self.snapshot()
        self._apply_changes(changes)
        self._record(before)

    def move(self, clip_id, start, owner=None):
        with self._lock:
            self._move([clip_id], start, owner)

    def move_many(self, clip_ids, start, owner=None):
        with self._lock:
            ids = list(dict.fromkeys(clip_ids))
            if not ids:
                return
            self._move(ids, start, owner)

    def move_group(self, group_id, start, owner=None):
        with self._lock:
            self._claim(owner)
            try:
                group = self.groups[group_id]
            except KeyError:
                raise KeyError("unknown group: %r" % (group_id,)) from None
            if not group["clips"]:
                return
            changes = self._primary_changes(group["clips"], start)
            if not changes:
                return
            before = self.snapshot()
            self._apply_changes(changes)
            self._record(before)

    # ------------------------------------------------------------------
    # Ripple delete / overlap analysis / undo
    # ------------------------------------------------------------------
    def ripple_delete(self, clip_id, owner=None):
        with self._lock:
            self._claim(owner)
            deleted = self._require_clip(clip_id)
            deleted_track = self._require_track(deleted["track"])
            if deleted_track.get("locked"):
                raise ValueError("track %r is locked" % (deleted["track"],))

            before = self.snapshot()
            shift = deleted["duration"]
            affected_tracks = {deleted["track"]}
            affected_tracks.update(self._linked_tracks(deleted["track"]))

            changes = {}
            for other_id, clip in self.clips.items():
                if other_id == clip_id or clip["track"] not in affected_tracks:
                    continue
                if clip["start"] < deleted["start"]:
                    continue
                # A clip beginning inside the removed interval can only move
                # as far left as time zero.
                changes[other_id] = max(0, clip["start"] - shift)

            self._apply_changes(changes)
            del self.clips[clip_id]
            for group in self.groups.values():
                if clip_id in group["clips"]:
                    group["clips"] = [item for item in group["clips"] if item != clip_id]
            self._record(before)

    def overlap_intervals(self):
        with self._lock:
            intervals = []
            by_track = {}
            for clip_id, clip in self.clips.items():
                by_track.setdefault(clip["track"], []).append(clip)

            for track_id, clips in by_track.items():
                clips.sort(key=lambda item: (item["start"], item["duration"], str(item["id"])))
                for index, first in enumerate(clips):
                    first_start = first["start"]
                    first_end = first_start + first["duration"]
                    for second in clips[index + 1 :]:
                        second_start = second["start"]
                        second_end = second_start + second["duration"]
                        if second_start >= first_end:
                            gap = second_start - first_end
                        else:
                            gap = 0
                        overlap = min(first_end, second_end) - max(first_start, second_start)
                        transition = max(
                            first.get("transition", 0), second.get("transition", 0)
                        )

                        if transition > 0 and (overlap > 0 or gap <= transition):
                            if overlap > 0:
                                center = (
                                    max(first_start, second_start)
                                    + min(first_end, second_end)
                                ) // 2
                            else:
                                center = (first_end + second_start) // 2
                            start = center - transition // 2
                            end = start + transition
                            intervals.append(
                                {
                                    "a": first["id"],
                                    "b": second["id"],
                                    "track": track_id,
                                    "kind": "transition",
                                    "frames": transition,
                                    "start": start,
                                    "end": end,
                                }
                            )
                        elif overlap > 0:
                            intervals.append(
                                {
                                    "a": first["id"],
                                    "b": second["id"],
                                    "track": track_id,
                                    "kind": "conflict",
                                    "frames": overlap,
                                    "start": max(first_start, second_start),
                                    "end": min(first_end, second_end),
                                }
                            )
            return intervals

    def undo(self):
        with self._lock:
            if self._edit_owner is not None:
                raise RuntimeError(
                    "timeline edit already owned by %r" % (self._edit_owner,)
                )
            if not self.history:
                return False
            entry = self.history.pop()
            self._restore(entry["before"])
            return True

    # ------------------------------------------------------------------
    # Zoom conversion
    # ------------------------------------------------------------------
    def pixels_to_frames(self, pixels, zoom_num, zoom_den=1):
        pixels = self._fraction(pixels)
        zoom_num = self._fraction(zoom_num)
        zoom_den = self._fraction(zoom_den)
        if zoom_num == 0:
            raise ValueError("zoom_num cannot be zero")
        if zoom_den == 0:
            raise ValueError("zoom_den cannot be zero")
        frames = (
            pixels
            * self.fps_num
            * zoom_den
            / (self.fps_den * zoom_num)
        )
        return self._round_half_up(frames)
