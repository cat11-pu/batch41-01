"""Semantic expression retargeting between facial rigs."""

from __future__ import annotations

import copy
import math
import re
import threading
import time


class ExpressionRetarget:
    """Map source facial shapes onto a target rig and blend the result.

    Source weights are semantic names.  ``mapping`` translates those names to
    the target rig; shapes that are not present in ``mapping`` use identity
    mapping when the target rig has a shape with the same name.  Expression
    and viseme weights stay in separate layers until they are combined.
    """

    _MODE_NORMALIZED = "normalized"
    _MODE_RAW = "raw"
    _SIDE_RE = re.compile(r"(left|right)", re.IGNORECASE)
    _EXPRESSION_KEYS = ("expression", "expressions", "weights")
    _VISEME_KEYS = ("viseme", "visemes")

    def __init__(self, source_shapes, target_shapes, mapping=None, mirrored=False):
        self.source_shapes = list(source_shapes)
        self.target_shapes = list(target_shapes)
        self.mapping = dict(mapping or {})
        self.mirrored = bool(mirrored)
        self.last = {}

        self._source_set = set(self.source_shapes)
        self._target_set = set(self.target_shapes)
        self._zero_weights = {name: 0.0 for name in self.target_shapes}
        self._resolved_mapping = {
            name: self._resolve_target(name) for name in self.source_shapes
        }

        self._lock = threading.RLock()
        self._buffers = [None, None]
        self._active_buffer = 0
        self._frames = 0
        self._warning_frames = 0
        self._warning_count = 0
        self._total_elapsed_ms = 0.0
        self._last_elapsed_ms = 0.0
        self._max_elapsed_ms = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def retarget(self, frame, mode=_MODE_RAW):
        """Map one source frame to the target rig.

        ``mode`` controls the final layer combination.  Raw mode (the default
        for incoming frames) preserves the values supplied by the tracker.
        Normalized mode normalizes each non-empty layer independently.
        """
        started = time.perf_counter()
        expression, viseme, head = self._extract_frame(frame)
        self._validate_layer(expression)
        self._validate_layer(viseme)

        mapped_expression, expression_warnings = self._map_layer(expression)
        mapped_viseme, viseme_warnings = self._map_layer(viseme)

        result = self.blend(mapped_expression, mapped_viseme, mode=mode)
        result["warnings"] = self._dedupe(
            expression_warnings + viseme_warnings + result["warnings"]
        )
        result["head"] = copy.deepcopy(head)
        result["mode"] = mode

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result["elapsed_ms"] = elapsed_ms
        self._commit(result, elapsed_ms)
        return result

    def retarget_at(self, frame, time_ms, next_frame, next_time_ms, at_ms):
        """Interpolate two source frames and retarget the interpolated frame."""
        time_ms = self._number(time_ms, "time_ms")
        next_time_ms = self._number(next_time_ms, "next_time_ms")
        at_ms = self._number(at_ms, "at_ms")

        expression_a, viseme_a, head_a = self._extract_frame(frame)
        expression_b, viseme_b, head_b = self._extract_frame(next_frame)
        self._validate_layer(expression_a)
        self._validate_layer(viseme_a)
        self._validate_layer(expression_b)
        self._validate_layer(viseme_b)

        span = next_time_ms - time_ms
        if span == 0.0:
            amount = 1.0 if at_ms >= next_time_ms else 0.0
        else:
            amount = (at_ms - time_ms) / span
        amount = min(1.0, max(0.0, amount))

        interpolated = {
            "weights": self._interpolate_weights(expression_a, expression_b, amount)
        }
        viseme = self._interpolate_weights(viseme_a, viseme_b, amount)
        if viseme:
            interpolated["viseme"] = viseme

        head = self._interpolate_values(head_a, head_b, amount)
        if head:
            interpolated["head"] = head

        return self.retarget(interpolated)

    def blend(self, expression, viseme, mode=_MODE_NORMALIZED):
        """Combine expression and viseme layers without a global clamp."""
        started = time.perf_counter()
        self._validate_mode(mode)

        expression_layer, expression_warnings = self._prepare_layer(expression)
        viseme_layer, viseme_warnings = self._prepare_layer(viseme)

        if mode == self._MODE_NORMALIZED:
            expression_layer = self._normalize_layer(expression_layer)
            viseme_layer = self._normalize_layer(viseme_layer)

        weights = {
            name: expression_layer[name] + viseme_layer[name]
            for name in self.target_shapes
        }
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "weights": weights,
            "layers": {
                "expression": expression_layer,
                "viseme": viseme_layer,
            },
            "total": sum(weights.values()),
            "warnings": self._dedupe(expression_warnings + viseme_warnings),
            "elapsed_ms": elapsed_ms,
        }

    def snapshot(self):
        """Return an isolated copy of the most recently committed frame."""
        with self._lock:
            buffer = self._buffers[self._active_buffer]
            if buffer is None:
                return {}
            return copy.deepcopy(buffer)

    def stats(self):
        """Return retargeting counters and elapsed-time statistics."""
        with self._lock:
            frames = self._frames
            total_elapsed_ms = self._total_elapsed_ms
            return {
                "frames": frames,
                "warning_frames": self._warning_frames,
                "warnings": self._warning_count,
                "last_elapsed_ms": self._last_elapsed_ms,
                "total_elapsed_ms": total_elapsed_ms,
                "avg_elapsed_ms": total_elapsed_ms / frames if frames else 0.0,
                "max_elapsed_ms": self._max_elapsed_ms,
            }

    # ------------------------------------------------------------------
    # Frame validation and mapping
    # ------------------------------------------------------------------
    def _extract_frame(self, frame):
        if not isinstance(frame, dict) or not frame:
            raise ValueError("empty expression frame")

        expression = self._first_present(frame, self._EXPRESSION_KEYS)
        viseme = self._first_present(frame, self._VISEME_KEYS)
        expression = {} if expression is None else expression
        viseme = {} if viseme is None else viseme

        if not isinstance(expression, dict) or not isinstance(viseme, dict):
            raise ValueError("frame layers must be dictionaries")
        if not expression and not viseme:
            raise ValueError("empty expression frame")

        head = frame.get("head", {})
        if head is None:
            head = {}
        if not isinstance(head, dict):
            raise ValueError("head pose must be a dictionary")
        return expression, viseme, head

    @staticmethod
    def _first_present(container, keys):
        for key in keys:
            if key in container:
                return container[key]
        return None

    def _validate_layer(self, layer):
        if not isinstance(layer, dict):
            raise ValueError("expression layer must be a dictionary")
        for name, value in layer.items():
            if not isinstance(name, str) or name not in self._source_set:
                raise ValueError(f"unknown source shape: {name!r}")
            self._number(value, f"weight for {name!r}")

    def _map_layer(self, layer):
        mapped = dict(self._zero_weights)
        warnings = []
        for source_name, value in layer.items():
            target_name = self._resolved_mapping[source_name]
            if target_name is None:
                missing = self._target_name(source_name)
                if missing not in warnings:
                    warnings.append(missing)
                continue
            mapped[target_name] += self._number(
                value, f"weight for {source_name!r}"
            )
        return mapped, warnings

    def _resolve_target(self, source_name, flip=True):
        target_name = self._target_name(source_name, flip=flip)
        return target_name if target_name in self._target_set else None

    def _target_name(self, source_name, flip=True):
        target_name = self.mapping.get(source_name, source_name)
        if not isinstance(target_name, str):
            raise ValueError(f"mapping target for {source_name!r} must be a string")
        if self.mirrored and flip:
            target_name = self._mirror_name(target_name)
        return target_name

    @classmethod
    def _mirror_name(cls, name):
        def swap(match):
            token = match.group(0)
            mirrored = "right" if token.lower() == "left" else "left"
            if token.isupper():
                return mirrored.upper()
            if token[:1].isupper():
                return mirrored.capitalize()
            return mirrored

        return cls._SIDE_RE.sub(swap, name)

    # ------------------------------------------------------------------
    # Layer preparation and interpolation
    # ------------------------------------------------------------------
    def _prepare_layer(self, layer):
        if layer is None:
            layer = {}
        if not isinstance(layer, dict):
            raise ValueError("layer must be a dictionary")

        prepared = dict(self._zero_weights)
        warnings = []
        for name, value in layer.items():
            if not isinstance(name, str) or name not in self._target_set:
                if name not in warnings:
                    warnings.append(name)
                continue
            prepared[name] = self._number(value, f"weight for {name!r}")
        return prepared, warnings

    @staticmethod
    def _normalize_layer(layer):
        total = sum(layer.values())
        if total == 0.0:
            return layer
        return {name: value / total for name, value in layer.items()}

    def _interpolate_weights(self, first, second, amount):
        result = {}
        for name in set(first) | set(second):
            start = self._number(first.get(name, 0.0), f"weight for {name!r}")
            end = self._number(second.get(name, 0.0), f"weight for {name!r}")
            result[name] = start + (end - start) * amount
        return result

    @classmethod
    def _interpolate_values(cls, first, second, amount):
        result = {}
        for name in set(first) | set(second):
            start = first.get(name)
            end = second.get(name)
            if cls._is_number(start) and cls._is_number(end):
                result[name] = start + (end - start) * amount
            else:
                result[name] = copy.deepcopy(end if amount >= 0.5 else start)
        return result

    # ------------------------------------------------------------------
    # Commit, helpers, and timing
    # ------------------------------------------------------------------
    def _commit(self, result, elapsed_ms):
        stored = copy.deepcopy(result)
        with self._lock:
            self._frames += 1
            self._total_elapsed_ms += elapsed_ms
            self._last_elapsed_ms = elapsed_ms
            self._max_elapsed_ms = max(self._max_elapsed_ms, elapsed_ms)
            self._warning_count += len(result["warnings"])
            if result["warnings"]:
                self._warning_frames += 1

            next_buffer = 1 - self._active_buffer
            self._buffers[next_buffer] = stored
            self._active_buffer = next_buffer
            self.last = stored

    @classmethod
    def _validate_mode(cls, mode):
        if mode not in (cls._MODE_NORMALIZED, cls._MODE_RAW):
            raise ValueError(f"unknown blend mode: {mode!r}")

    @classmethod
    def _number(cls, value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a finite number")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{label} must be a finite number")
        return value

    @staticmethod
    def _is_number(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    @staticmethod
    def _dedupe(values):
        result = []
        for value in values:
            if value not in result:
                result.append(value)
        return result
