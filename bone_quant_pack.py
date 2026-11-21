"""Bone animation quantization packer.

Contract used by the tests
--------------------------
Clip input: ``{"id", "fps", "duration",
"frames": [{"t", "bones": {name: {"rot": (w, x, y, z),
"trans": (x, y, z), "scale": (x, y, z)}}}]}``.

* Quaternions are normalized and their sign is flipped so the dropped (largest)
  component is positive (ties: lowest index).  The dropped component index is
  stored in 2 bits (0..3), the other three components are quantized with
  ``comp_bits`` (default 8) over [-1, 1].  Decoding rebuilds a unit quaternion
  whose angle to the source is below 2 degrees for the test rotations.
* ``set_bone_range(name, low, high)`` sets a per-bone translation range
  (``low < high`` required, otherwise ``ValueError``); translations are
  clamped to it and quantized with ``trans_bits`` (default 8).  Bones without
  an explicit range use the full [-10, 10] range.
* Scale is quantized with ``scale_bits`` (default 8) over [0.5, 1.5].
* A frame is a key frame when its index is a multiple of
  ``max(1, round(key_interval * fps))``; every other frame is a delta frame
  with ``"key": False``.
* ``encode`` rejects non-finite rotations, translations or scales with
  ``ValueError`` and is deterministic: the same clip encodes identically.
* ``decode`` returns a clip with the same frame count and duration.
* ``encode_batch(clips, budget_ms=200.0)`` encodes at most ``max_batch`` (200)
  clips, estimates ``estimated_ms = 0.5 * total frames`` and reports
  ``within_budget``; more than ``max_batch`` clips raise ``ValueError``.

Key frames store absolute quantized codes.  Delta frames store integer code
residuals relative to the previous frame, so quantization error never
accumulates between key frames.  ``stats`` reports cumulative encode/decode
counts, frame counts, byte counts, and the largest reconstruction errors seen.
"""

import json
import math


def _as_finite_float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a finite number" % label) from None
    if not math.isfinite(result):
        raise ValueError("%s must be a finite number" % label)
    return result


def _validate_bits(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % label)
    if value < 1 or value > 30:
        raise ValueError("%s must be between 1 and 30" % label)
    return value


def _as_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % label)
    return value


def _normalize_quaternion(rotation):
    if not isinstance(rotation, (tuple, list)) or len(rotation) != 4:
        raise ValueError("rotation must contain four components")
    values = [_as_finite_float(value, "rotation component") for value in rotation]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0 or not math.isfinite(norm):
        raise ValueError("rotation must have a non-zero finite norm")
    values = [value / norm for value in values]
    index = max(range(4), key=lambda axis: (abs(values[axis]), -axis))
    if values[index] < 0.0:
        values = [-value for value in values]
    values = [0.0 if value == 0.0 else value for value in values]
    return tuple(values), index


def _quantize(value, maximum):
    scaled = int(round((value + 1.0) * 0.5 * maximum))
    return min(maximum, max(0, scaled))


def _dequantize(code, maximum):
    return (2.0 * code / maximum) - 1.0


def _quantize_range(value, low, high, maximum):
    if value <= low:
        return 0
    if value >= high:
        return maximum
    scaled = int(round(((value - low) / (high - low)) * maximum))
    return min(maximum, max(0, scaled))


def _dequantize_range(code, low, high, maximum):
    return low + ((high - low) * code / maximum)


def _quaternion_angle(first, second):
    dot = abs(sum(left * right for left, right in zip(first, second)))
    return 2.0 * math.acos(min(1.0, max(-1.0, dot)))


class BoneQuantPack:
    def __init__(self, key_interval=0.1, comp_bits=8, trans_bits=8, scale_bits=8, max_batch=200):
        self.key_interval = _as_finite_float(key_interval, "key_interval")
        if self.key_interval < 0.0:
            raise ValueError("key_interval must not be negative")
        self.comp_bits = _validate_bits(comp_bits, "comp_bits")
        self.trans_bits = _validate_bits(trans_bits, "trans_bits")
        self.scale_bits = _validate_bits(scale_bits, "scale_bits")
        if isinstance(max_batch, bool) or not isinstance(max_batch, int) or max_batch < 1:
            raise ValueError("max_batch must be a positive integer")
        self.max_batch = max_batch
        self._bone_ranges = {}
        self._bone_scale_bits = {}
        self.reset_stats()

    def reset_stats(self):
        self._encoded_clips = 0
        self._decoded_clips = 0
        self._encoded_frames = 0
        self._decoded_frames = 0
        self._key_frames = 0
        self._delta_frames = 0
        self._encoded_bytes = 0
        self._decoded_bytes = 0
        self._last_encoded_frames = 0
        self._last_encoded_bytes = 0
        self._last_decoded_frames = 0
        self._max_rotation_error_deg = 0.0
        self._last_max_rotation_error_deg = 0.0
        self._max_translation_error = 0.0
        self._last_max_translation_error = 0.0
        self._max_scale_error = 0.0
        self._last_max_scale_error = 0.0
        self._batch_encoded = 0
        self._batch_estimated_ms = 0.0
        self._batch_within_budget = True

    def set_bone_range(self, name, low, high, scale_bits=None):
        if not isinstance(name, str) or not name:
            raise ValueError("bone name must be a non-empty string")
        low = _as_finite_float(low, "bone range low")
        high = _as_finite_float(high, "bone range high")
        if not low < high:
            raise ValueError("bone range must satisfy low < high")
        self._bone_ranges[name] = (low, high)
        if scale_bits is not None:
            self.set_bone_scale_bits(name, scale_bits)

    def set_bone_scale_bits(self, name, bits):
        if not isinstance(name, str) or not name:
            raise ValueError("bone name must be a non-empty string")
        self._bone_scale_bits[name] = _validate_bits(bits, "scale_bits")

    def _range_for(self, name):
        return self._bone_ranges.get(name, (-10.0, 10.0))

    def _scale_bits_for(self, name):
        return self._bone_scale_bits.get(name, self.scale_bits)

    def _reconstruct_quaternion(self, codes, index, comp_bits):
        maximum = (1 << comp_bits) - 1
        values = [0.0, 0.0, 0.0, 0.0]
        remainder = 0.0
        for axis in range(4):
            if axis == index:
                continue
            value = _dequantize(codes[axis], maximum)
            values[axis] = value
            remainder += value * value
        values[index] = math.sqrt(max(0.0, 1.0 - remainder))
        norm = math.sqrt(sum(value * value for value in values))
        if norm <= 0.0 or not math.isfinite(norm):
            raise ValueError("quantized rotation cannot be reconstructed")
        values = [value / norm for value in values]
        if values[index] < 0.0:
            values = [-value for value in values]
        values = [0.0 if value == 0.0 else value for value in values]
        return tuple(values)

    def _absolute_state(self, name, data):
        if not isinstance(data, dict):
            raise ValueError("bone data must be an object")
        rotation, index = _normalize_quaternion(data.get("rot"))
        trans = data.get("trans")
        scale = data.get("scale")
        if not isinstance(trans, (tuple, list)) or len(trans) != 3:
            raise ValueError("translation must contain three components")
        if not isinstance(scale, (tuple, list)) or len(scale) != 3:
            raise ValueError("scale must contain three components")
        trans_values = [_as_finite_float(value, "translation component") for value in trans]
        scale_values = [_as_finite_float(value, "scale component") for value in scale]

        comp_maximum = (1 << self.comp_bits) - 1
        trans_maximum = (1 << self.trans_bits) - 1
        scale_bits = self._scale_bits_for(name)
        scale_maximum = (1 << scale_bits) - 1
        low, high = self._range_for(name)

        rot_codes = [0, 0, 0, 0]
        stored_rot = []
        remainder = 0.0
        for axis in range(4):
            if axis == index:
                continue
            code = _quantize(rotation[axis], comp_maximum)
            rot_codes[axis] = code
            stored_rot.append(code)
            value = _dequantize(code, comp_maximum)
            remainder += value * value
        rot_codes[index] = _quantize(
            math.sqrt(max(0.0, 1.0 - remainder)), comp_maximum
        )

        trans_codes = [
            _quantize_range(value, low, high, trans_maximum) for value in trans_values
        ]
        scale_codes = [
            _quantize_range(min(1.5, max(0.5, value)), 0.5, 1.5, scale_maximum)
            for value in scale_values
        ]

        state = {
            "index": index,
            "rot_codes": tuple(rot_codes),
            "trans_codes": tuple(trans_codes),
            "scale_codes": tuple(scale_codes),
            "scale_bits": scale_bits,
        }
        payload = {
            "mode": "key",
            "index": index,
            "rot": tuple(stored_rot),
            "trans": tuple(trans_codes),
            "scale": tuple(scale_codes),
        }

        decoded_rotation = self._reconstruct_quaternion(
            rot_codes, index, self.comp_bits
        )
        decoded_trans = [
            _dequantize_range(code, low, high, trans_maximum) for code in trans_codes
        ]
        decoded_scale = [
            _dequantize_range(code, 0.5, 1.5, scale_maximum) for code in scale_codes
        ]
        return (
            payload,
            state,
            rotation,
            decoded_rotation,
            trans_values,
            decoded_trans,
            scale_values,
            decoded_scale,
        )

    def _delta_payload(self, state, previous):
        return {
            "mode": "delta",
            "index": state["index"],
            "rot_delta": tuple(
                state["rot_codes"][axis] - previous["rot_codes"][axis]
                for axis in range(4)
            ),
            "trans_delta": tuple(
                state["trans_codes"][axis] - previous["trans_codes"][axis]
                for axis in range(3)
            ),
            "scale_delta": tuple(
                state["scale_codes"][axis] - previous["scale_codes"][axis]
                for axis in range(3)
            ),
        }

    def encode(self, clip):
        if not isinstance(clip, dict):
            raise ValueError("clip must be an object")
        frames = clip.get("frames")
        if not isinstance(frames, list):
            raise ValueError("clip frames must be a list")
        fps = _as_finite_float(clip.get("fps"), "fps")
        if fps <= 0.0:
            raise ValueError("fps must be positive")
        duration = _as_finite_float(clip.get("duration"), "duration")
        if duration < 0.0:
            raise ValueError("duration must not be negative")

        stride = max(1, round(self.key_interval * fps))
        packed_frames = []
        previous_bones = {}
        last_rotation_error = 0.0
        last_translation_error = 0.0
        last_scale_error = 0.0
        key_count = 0
        delta_count = 0

        for frame_index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                raise ValueError("frame must be an object")
            bones = frame.get("bones")
            if not isinstance(bones, dict):
                raise ValueError("frame bones must be an object")
            timestamp = _as_finite_float(frame.get("t"), "frame time")
            is_key = frame_index % stride == 0
            if is_key:
                key_count += 1
            else:
                delta_count += 1

            packed_bones = {}
            current_bones = {}
            for name, data in bones.items():
                (
                    absolute_payload,
                    state,
                    source_rotation,
                    decoded_rotation,
                    source_trans,
                    decoded_trans,
                    source_scale,
                    decoded_scale,
                ) = self._absolute_state(name, data)
                previous = previous_bones.get(name)
                if is_key or previous is None:
                    payload = absolute_payload
                else:
                    payload = self._delta_payload(state, previous)
                packed_bones[name] = payload
                current_bones[name] = state

                last_rotation_error = max(
                    last_rotation_error,
                    _quaternion_angle(source_rotation, decoded_rotation),
                )
                last_translation_error = max(
                    last_translation_error,
                    max(
                        abs(left - right)
                        for left, right in zip(source_trans, decoded_trans)
                    ),
                )
                last_scale_error = max(
                    last_scale_error,
                    max(
                        abs(left - right)
                        for left, right in zip(source_scale, decoded_scale)
                    ),
                )

            packed_frames.append(
                {"t": timestamp, "key": is_key, "bones": packed_bones}
            )
            previous_bones = current_bones

        packed = {
            "id": clip.get("id"),
            "fps": fps,
            "duration": duration,
            "frame_count": len(frames),
            "comp_bits": self.comp_bits,
            "trans_bits": self.trans_bits,
            "scale_bits": self.scale_bits,
            "bone_ranges": {
                name: [low, high] for name, (low, high) in self._bone_ranges.items()
            },
            "bone_scale_bits": dict(self._bone_scale_bits),
            "frames": packed_frames,
        }

        encoded_bytes = len(
            json.dumps(
                packed, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
        self._encoded_clips += 1
        self._encoded_frames += len(frames)
        self._key_frames += key_count
        self._delta_frames += delta_count
        self._encoded_bytes += encoded_bytes
        self._last_encoded_frames = len(frames)
        self._last_encoded_bytes = encoded_bytes
        self._last_max_rotation_error_deg = math.degrees(last_rotation_error)
        self._last_max_translation_error = last_translation_error
        self._last_max_scale_error = last_scale_error
        self._max_rotation_error_deg = max(
            self._max_rotation_error_deg, self._last_max_rotation_error_deg
        )
        self._max_translation_error = max(
            self._max_translation_error, last_translation_error
        )
        self._max_scale_error = max(self._max_scale_error, last_scale_error)
        return packed

    def _validate_codes(self, values, label, maximum):
        if not isinstance(values, (tuple, list)) or len(values) != 3:
            raise ValueError("%s must contain three codes" % label)
        result = []
        for value in values:
            code = _as_int(value, label)
            if code < 0 or code > maximum:
                raise ValueError("%s code is out of range" % label)
            result.append(code)
        return result

    def _decode_key_state(
        self,
        name,
        payload,
        comp_bits,
        trans_bits,
        default_scale_bits,
        scale_bits_map,
    ):
        index = _as_int(payload.get("index"), "rotation index")
        if index < 0 or index > 3:
            raise ValueError("rotation index is out of range")
        comp_maximum = (1 << comp_bits) - 1
        stored_rot = self._validate_codes(
            payload.get("rot"), "rotation", comp_maximum
        )
        rot_codes = [0, 0, 0, 0]
        stored_index = 0
        remainder = 0.0
        for axis in range(4):
            if axis == index:
                continue
            code = stored_rot[stored_index]
            stored_index += 1
            rot_codes[axis] = code
            value = _dequantize(code, comp_maximum)
            remainder += value * value
        rot_codes[index] = _quantize(
            math.sqrt(max(0.0, 1.0 - remainder)), comp_maximum
        )

        trans_maximum = (1 << trans_bits) - 1
        trans_codes = self._validate_codes(
            payload.get("trans"), "translation", trans_maximum
        )
        scale_bits = scale_bits_map.get(name, default_scale_bits)
        scale_maximum = (1 << scale_bits) - 1
        scale_codes = self._validate_codes(
            payload.get("scale"), "scale", scale_maximum
        )
        return {
            "index": index,
            "rot_codes": tuple(rot_codes),
            "trans_codes": tuple(trans_codes),
            "scale_codes": tuple(scale_codes),
            "scale_bits": scale_bits,
        }

    def _decode_delta_state(
        self,
        name,
        payload,
        previous,
        comp_bits,
        trans_bits,
        default_scale_bits,
        scale_bits_map,
    ):
        if previous is None:
            raise ValueError("delta frame has no previous bone state")
        index = _as_int(payload.get("index"), "rotation index")
        if index < 0 or index > 3:
            raise ValueError("rotation index is out of range")

        rot_delta = payload.get("rot_delta")
        trans_delta = payload.get("trans_delta")
        scale_delta = payload.get("scale_delta")
        if not isinstance(rot_delta, (tuple, list)) or len(rot_delta) != 4:
            raise ValueError("rotation delta must contain four codes")
        if not isinstance(trans_delta, (tuple, list)) or len(trans_delta) != 3:
            raise ValueError("translation delta must contain three codes")
        if not isinstance(scale_delta, (tuple, list)) or len(scale_delta) != 3:
            raise ValueError("scale delta must contain three codes")

        comp_maximum = (1 << comp_bits) - 1
        trans_maximum = (1 << trans_bits) - 1
        scale_bits = scale_bits_map.get(name, default_scale_bits)
        scale_maximum = (1 << scale_bits) - 1

        rot_codes = []
        for axis in range(4):
            code = previous["rot_codes"][axis] + _as_int(
                rot_delta[axis], "rotation delta"
            )
            if code < 0 or code > comp_maximum:
                raise ValueError("rotation delta result is out of range")
            rot_codes.append(code)

        trans_codes = []
        for axis in range(3):
            code = previous["trans_codes"][axis] + _as_int(
                trans_delta[axis], "translation delta"
            )
            if code < 0 or code > trans_maximum:
                raise ValueError("translation delta result is out of range")
            trans_codes.append(code)

        scale_codes = []
        for axis in range(3):
            code = previous["scale_codes"][axis] + _as_int(
                scale_delta[axis], "scale delta"
            )
            if code < 0 or code > scale_maximum:
                raise ValueError("scale delta result is out of range")
            scale_codes.append(code)

        return {
            "index": index,
            "rot_codes": tuple(rot_codes),
            "trans_codes": tuple(trans_codes),
            "scale_codes": tuple(scale_codes),
            "scale_bits": scale_bits,
        }

    def decode(self, packed):
        if not isinstance(packed, dict):
            raise ValueError("packed clip must be an object")
        frames = packed.get("frames")
        if not isinstance(frames, list):
            raise ValueError("packed frames must be a list")
        if "frame_count" in packed:
            frame_count = _as_int(packed["frame_count"], "frame count")
            if frame_count != len(frames):
                raise ValueError("packed frame count does not match frames")

        fps = _as_finite_float(packed.get("fps"), "fps")
        if fps <= 0.0:
            raise ValueError("fps must be positive")
        duration = _as_finite_float(packed.get("duration"), "duration")
        if duration < 0.0:
            raise ValueError("duration must not be negative")
        comp_bits = _validate_bits(
            packed.get("comp_bits", self.comp_bits), "comp_bits"
        )
        trans_bits = _validate_bits(
            packed.get("trans_bits", self.trans_bits), "trans_bits"
        )
        default_scale_bits = _validate_bits(
            packed.get("scale_bits", self.scale_bits), "scale_bits"
        )

        ranges = {}
        raw_ranges = packed.get("bone_ranges", {})
        if not isinstance(raw_ranges, dict):
            raise ValueError("bone_ranges must be an object")
        for name, pair in raw_ranges.items():
            if (
                not isinstance(name, str)
                or not isinstance(pair, (tuple, list))
                or len(pair) != 2
            ):
                raise ValueError("invalid bone range")
            low = _as_finite_float(pair[0], "bone range low")
            high = _as_finite_float(pair[1], "bone range high")
            if not low < high:
                raise ValueError("bone range must satisfy low < high")
            ranges[name] = (low, high)

        scale_bits_map = {}
        raw_scale_bits = packed.get("bone_scale_bits", {})
        if not isinstance(raw_scale_bits, dict):
            raise ValueError("bone_scale_bits must be an object")
        for name, bits in raw_scale_bits.items():
            if not isinstance(name, str):
                raise ValueError("invalid bone name")
            scale_bits_map[name] = _validate_bits(bits, "scale_bits")

        previous_bones = {}
        decoded_frames = []
        for frame in frames:
            if not isinstance(frame, dict):
                raise ValueError("packed frame must be an object")
            timestamp = _as_finite_float(frame.get("t"), "frame time")
            is_key = frame.get("key")
            if not isinstance(is_key, bool):
                raise ValueError("frame key flag must be a boolean")
            bones = frame.get("bones")
            if not isinstance(bones, dict):
                raise ValueError("packed frame bones must be an object")

            decoded_bones = {}
            current_bones = {}
            for name, payload in bones.items():
                if not isinstance(name, str) or not isinstance(payload, dict):
                    raise ValueError("invalid packed bone")
                mode = payload.get("mode")
                if mode is None:
                    mode = "delta" if "rot_delta" in payload else "key"
                if mode == "key":
                    state = self._decode_key_state(
                        name,
                        payload,
                        comp_bits,
                        trans_bits,
                        default_scale_bits,
                        scale_bits_map,
                    )
                elif mode == "delta":
                    state = self._decode_delta_state(
                        name,
                        payload,
                        previous_bones.get(name),
                        comp_bits,
                        trans_bits,
                        default_scale_bits,
                        scale_bits_map,
                    )
                else:
                    raise ValueError("unknown packed bone mode")

                index = state["index"]
                rotation = self._reconstruct_quaternion(
                    state["rot_codes"], index, comp_bits
                )
                low, high = ranges.get(name, (-10.0, 10.0))
                trans_maximum = (1 << trans_bits) - 1
                translation = tuple(
                    _dequantize_range(code, low, high, trans_maximum)
                    for code in state["trans_codes"]
                )
                scale_bits = scale_bits_map.get(name, default_scale_bits)
                scale_maximum = (1 << scale_bits) - 1
                scale = tuple(
                    _dequantize_range(code, 0.5, 1.5, scale_maximum)
                    for code in state["scale_codes"]
                )
                decoded_bones[name] = {
                    "rot": rotation,
                    "trans": translation,
                    "scale": scale,
                }
                current_bones[name] = state

            previous_bones = current_bones
            decoded_frames.append({"t": timestamp, "bones": decoded_bones})

        result = {
            "id": packed.get("id"),
            "fps": fps,
            "duration": duration,
            "frames": decoded_frames,
        }
        decoded_bytes = len(
            json.dumps(
                result, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        )
        self._decoded_clips += 1
        self._decoded_frames += len(decoded_frames)
        self._decoded_bytes += decoded_bytes
        self._last_decoded_frames = len(decoded_frames)
        return result

    def encode_batch(self, clips, budget_ms=200.0):
        budget = _as_finite_float(budget_ms, "budget_ms")
        if budget < 0.0:
            raise ValueError("budget_ms must not be negative")
        try:
            clip_list = list(clips)
        except TypeError:
            raise ValueError("clips must be iterable") from None
        if len(clip_list) > self.max_batch:
            raise ValueError("batch exceeds max_batch")

        packed = [self.encode(clip) for clip in clip_list]
        total_frames = sum(len(clip["frames"]) for clip in clip_list)
        estimated_ms = 0.5 * total_frames
        within_budget = estimated_ms <= budget
        self._batch_encoded = len(packed)
        self._batch_estimated_ms = estimated_ms
        self._batch_within_budget = within_budget
        return {
            "encoded": len(packed),
            "estimated_ms": estimated_ms,
            "within_budget": within_budget,
            "clips": packed,
        }

    def stats(self):
        return {
            "encoded_clips": self._encoded_clips,
            "decoded_clips": self._decoded_clips,
            "encoded_frames": self._encoded_frames,
            "decoded_frames": self._decoded_frames,
            "key_frames": self._key_frames,
            "delta_frames": self._delta_frames,
            "encoded_bytes": self._encoded_bytes,
            "decoded_bytes": self._decoded_bytes,
            "last_encoded_frames": self._last_encoded_frames,
            "last_encoded_bytes": self._last_encoded_bytes,
            "last_decoded_frames": self._last_decoded_frames,
            "max_rotation_error_deg": self._max_rotation_error_deg,
            "last_max_rotation_error_deg": self._last_max_rotation_error_deg,
            "max_translation_error": self._max_translation_error,
            "last_max_translation_error": self._last_max_translation_error,
            "max_scale_error": self._max_scale_error,
            "last_max_scale_error": self._last_max_scale_error,
            "batch_encoded": self._batch_encoded,
            "batch_estimated_ms": self._batch_estimated_ms,
            "batch_within_budget": self._batch_within_budget,
            "clips": self._encoded_clips,
            "frames": self._encoded_frames,
            "bytes": self._encoded_bytes,
        }
