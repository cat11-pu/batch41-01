"""Two-point foot IK with pelvis adaptation and local-space locking."""

import math
import threading
import time


_SIDES = ("left", "right")
_UP_NORMAL_EPSILON = 1e-6
_EPSILON = 1e-9


class FootIkLock:
    """Stateful, thread-safe foot IK solver for a two-legged character.

    Ground queries receive the world-space X/Z position of the toe and heel
    samples and return ``(height, normal)``. Foot locks are retained in the
    character's local space so moving or turning the character does not make a
    planted foot slide.
    """

    def __init__(self, leg_length=1.0, pelvis_max_drop=0.4,
                 slope_smooth_deg_per_s=90.0, pelvis_smooth_per_s=5.0,
                 hip_width=0.4):
        self.leg_length = self._positive_float(leg_length, "leg_length")
        self.pelvis_max_drop = self._non_negative_float(
            pelvis_max_drop, "pelvis_max_drop"
        )
        self.slope_smooth_deg_per_s = self._non_negative_float(
            slope_smooth_deg_per_s, "slope_smooth_deg_per_s"
        )
        self.pelvis_smooth_per_s = self._non_negative_float(
            pelvis_smooth_per_s, "pelvis_smooth_per_s"
        )
        self.hip_width = self._non_negative_float(hip_width, "hip_width")

        self._locked = {"left": False, "right": False}
        self._states = {"left": "unlocked", "right": "unlocked"}
        self._local = {"left": None, "right": None}
        self._last_pitch = {"left": 0.0, "right": 0.0}
        self._pitch_initialized = {"left": False, "right": False}
        self._pelvis_drop = 0.0
        self._pelvis_initialized = False
        self._pelvis_writes = 0
        self._last_elapsed_ms = 0.0
        self._last_release = None
        self._pelvis_snapshot = {
            "drop": 0.0,
            "position": None,
            "max_drop": self.pelvis_max_drop,
        }
        self._mutex = threading.RLock()

    @staticmethod
    def _to_float(value, name):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite number")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} must be a finite number")
        return number

    @classmethod
    def _positive_float(cls, value, name):
        number = cls._to_float(value, name)
        if number <= 0.0:
            raise ValueError(f"{name} must be greater than zero")
        return number

    @classmethod
    def _non_negative_float(cls, value, name):
        number = cls._to_float(value, name)
        if number < 0.0:
            raise ValueError(f"{name} must not be negative")
        return number

    @classmethod
    def _vector3(cls, value, name):
        if hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
            values = (value.x, value.y, value.z)
        else:
            try:
                values = tuple(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a 3D vector") from exc
        if len(values) < 3:
            raise ValueError(f"{name} must be a 3D vector")
        return (
            cls._to_float(values[0], f"{name}.x"),
            cls._to_float(values[1], f"{name}.y"),
            cls._to_float(values[2], f"{name}.z"),
        )

    @classmethod
    def _parse_feet(cls, feet):
        if not isinstance(feet, dict):
            raise ValueError("feet must contain left and right entries")
        parsed = {}
        for side in _SIDES:
            if side not in feet:
                raise ValueError(f"missing {side} foot")
            entry = feet[side]
            if not isinstance(entry, dict):
                raise ValueError(f"{side} foot must be a mapping")
            for key in ("ankle", "toe", "heel"):
                if key not in entry:
                    raise ValueError(f"missing {side}.{key}")
            parsed[side] = {
                "ankle": cls._vector3(entry["ankle"], f"{side}.ankle"),
                "toe": cls._vector3(entry["toe"], f"{side}.toe"),
                "heel": cls._vector3(entry["heel"], f"{side}.heel"),
            }
        return parsed

    @classmethod
    def _parse_character(cls, character):
        if not isinstance(character, dict) or "position" not in character:
            raise ValueError("character must contain a position")
        position = cls._vector3(character["position"], "character.position")
        yaw = cls._to_float(character.get("yaw_deg", 0.0), "character.yaw_deg")
        hip_height = cls._to_float(
            character.get("hip_height", 0.0), "character.hip_height"
        )
        return position, yaw, hip_height

    @staticmethod
    def _ground_sample(ground, x, z, name):
        try:
            sample = ground(x, z)
        except Exception as exc:
            raise ValueError(f"ground query failed for {name}") from exc
        if not isinstance(sample, (tuple, list)) or len(sample) < 2:
            raise ValueError(f"ground returned an invalid sample for {name}")
        height = FootIkLock._to_float(sample[0], f"ground.{name}.height")
        normal = FootIkLock._vector3(sample[1], f"ground.{name}.normal")
        length = math.sqrt(normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2)
        if length <= _EPSILON:
            raise ValueError(f"ground normal is zero for {name}")
        normal = (
            normal[0] / length,
            normal[1] / length,
            normal[2] / length,
        )
        return height, normal

    @staticmethod
    def _world_to_local(world, origin, cos_yaw, sin_yaw):
        dx = world[0] - origin[0]
        dy = world[1] - origin[1]
        dz = world[2] - origin[2]
        return (
            dx * cos_yaw - dz * sin_yaw,
            dy,
            dx * sin_yaw + dz * cos_yaw,
        )

    @staticmethod
    def _local_to_world(local, origin, cos_yaw, sin_yaw):
        return (
            origin[0] + local[0] * cos_yaw + local[2] * sin_yaw,
            origin[1] + local[1],
            origin[2] - local[0] * sin_yaw + local[2] * cos_yaw,
        )

    @staticmethod
    def _clamp(value, low, high):
        return low if value < low else high if value > high else value

    def _smooth_pitch(self, side, target, dt, first):
        if first:
            return target
        max_change = self.slope_smooth_deg_per_s * dt
        current = self._last_pitch[side]
        return current + self._clamp(target - current, -max_change, max_change)

    def _smooth_pelvis(self, target, dt):
        if not self._pelvis_initialized:
            return target
        if self.pelvis_smooth_per_s <= 0.0:
            return target
        max_change = self.pelvis_smooth_per_s * dt
        return self._pelvis_drop + self._clamp(
            target - self._pelvis_drop, -max_change, max_change
        )

    def _solve_leg(self, hip, target, pole):
        delta = (
            target[0] - hip[0],
            target[1] - hip[1],
            target[2] - hip[2],
        )
        distance = math.sqrt(delta[0] ** 2 + delta[1] ** 2 + delta[2] ** 2)
        if distance <= _EPSILON:
            weight = 1.0
            direction = (0.0, -1.0, 0.0)
        else:
            weight = 1.0 if distance <= self.leg_length else (
                self.leg_length / distance
            )
            direction = (
                delta[0] / distance,
                delta[1] / distance,
                delta[2] / distance,
            )

        half = self.leg_length * 0.5
        reach = min(distance, self.leg_length)
        cosine = (2.0 * half * half - reach * reach) / (2.0 * half * half)
        knee_angle = math.degrees(math.acos(self._clamp(cosine, -1.0, 1.0)))

        pole_length = math.sqrt(pole[0] ** 2 + pole[1] ** 2 + pole[2] ** 2)
        if pole_length <= _EPSILON:
            pole = (0.0, 0.0, 1.0)
            pole_length = 1.0
        pole = (pole[0] / pole_length, pole[1] / pole_length, pole[2] / pole_length)
        along = (
            direction[0] * pole[0]
            + direction[1] * pole[1]
            + direction[2] * pole[2]
        )
        bend = (
            pole[0] - direction[0] * along,
            pole[1] - direction[1] * along,
            pole[2] - direction[2] * along,
        )
        bend_length = math.sqrt(bend[0] ** 2 + bend[1] ** 2 + bend[2] ** 2)
        if bend_length <= _EPSILON:
            bend = (0.0, 1.0, 0.0)
            bend_length = 1.0
        bend = (
            bend[0] / bend_length,
            bend[1] / bend_length,
            bend[2] / bend_length,
        )

        along_reach = min(distance, self.leg_length)
        midpoint = (
            hip[0] + direction[0] * along_reach * 0.5,
            hip[1] + direction[1] * along_reach * 0.5,
            hip[2] + direction[2] * along_reach * 0.5,
        )
        knee_offset = math.sqrt(max(0.0, half * half - (reach * 0.5) ** 2))
        knee = (
            midpoint[0] + bend[0] * knee_offset,
            midpoint[1] + bend[1] * knee_offset,
            midpoint[2] + bend[2] * knee_offset,
        )
        knee_vector = (
            knee[0] - midpoint[0],
            knee[1] - midpoint[1],
            knee[2] - midpoint[2],
        )
        pole_dot = (
            knee_vector[0] * pole[0]
            + knee_vector[1] * pole[1]
            + knee_vector[2] * pole[2]
        )
        inverted = pole_dot < -_EPSILON or knee_angle < 0.0 or knee_angle > 180.0
        return weight, knee_angle, inverted, knee

    def solve(self, feet, character, ground, dt=1.0 / 60.0):
        started = time.perf_counter()
        with self._mutex:
            parsed_feet = self._parse_feet(feet)
            position, yaw_deg, hip_height = self._parse_character(character)
            dt = self._to_float(dt, "dt")
            if dt < 0.0:
                raise ValueError("dt must not be negative")
            if not callable(ground):
                raise ValueError("ground must be callable")

            yaw_rad = math.radians(yaw_deg)
            cos_yaw = math.cos(yaw_rad)
            sin_yaw = math.sin(yaw_rad)

            feet_info = {}
            for side in _SIDES:
                data = parsed_feet[side]
                ankle = data["ankle"]
                input_local = self._world_to_local(
                    ankle, position, cos_yaw, sin_yaw
                )

                was_locked = self._locked[side] and self._local[side] is not None
                if was_locked:
                    anchor = self._local_to_world(
                        self._local[side], position, cos_yaw, sin_yaw
                    )
                else:
                    anchor = ankle

                shift = (
                    anchor[0] - ankle[0],
                    anchor[1] - ankle[1],
                    anchor[2] - ankle[2],
                )
                toe = (
                    data["toe"][0] + shift[0],
                    data["toe"][1] + shift[1],
                    data["toe"][2] + shift[2],
                )
                heel = (
                    data["heel"][0] + shift[0],
                    data["heel"][1] + shift[1],
                    data["heel"][2] + shift[2],
                )

                toe_height, toe_normal = self._ground_sample(
                    ground, toe[0], toe[2], f"{side}.toe"
                )
                heel_height, heel_normal = self._ground_sample(
                    ground, heel[0], heel[2], f"{side}.heel"
                )

                toe_accepted = toe_normal[1] > _UP_NORMAL_EPSILON
                heel_accepted = heel_normal[1] > _UP_NORMAL_EPSILON
                corrections = []
                normals = []
                heights = []
                if toe_accepted:
                    corrections.append(toe_height - toe[1])
                    normals.append(toe_normal)
                    heights.append(toe_height)
                if heel_accepted:
                    corrections.append(heel_height - heel[1])
                    normals.append(heel_normal)
                    heights.append(heel_height)

                accepted = bool(corrections)
                if accepted:
                    lift = max(corrections)
                    normal = (
                        sum(item[0] for item in normals),
                        sum(item[1] for item in normals),
                        sum(item[2] for item in normals),
                    )
                    normal_length = math.sqrt(
                        normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2
                    )
                    normal = (
                        normal[0] / normal_length,
                        normal[1] / normal_length,
                        normal[2] / normal_length,
                    )
                    local_normal = self._world_to_local(
                        normal, (0.0, 0.0, 0.0), cos_yaw, sin_yaw
                    )
                    target_pitch = math.degrees(
                        math.atan2(local_normal[2], local_normal[1])
                    )
                    contact_height = min(heights)
                else:
                    lift = 0.0
                    target_pitch = 0.0
                    contact_height = None

                pitch = self._smooth_pitch(
                    side, target_pitch, dt, not self._pitch_initialized[side]
                )
                target = (anchor[0], anchor[1] + lift, anchor[2])
                offset = (
                    target[0] - ankle[0],
                    target[1] - ankle[1],
                    target[2] - ankle[2],
                )

                feet_info[side] = {
                    "ankle": ankle,
                    "anchor": anchor,
                    "input_local": input_local,
                    "was_locked": was_locked,
                    "accepted": accepted,
                    "pitch": pitch,
                    "target": target,
                    "offset": offset,
                    "contact_height": contact_height,
                    "normal": normal if accepted else None,
                    "toe": {
                        "accepted": toe_accepted,
                        "height": toe_height,
                        "normal": toe_normal,
                    },
                    "heel": {
                        "accepted": heel_accepted,
                        "height": heel_height,
                        "normal": heel_normal,
                    },
                }

            contact_heights = [
                info["contact_height"] for info in feet_info.values()
                if info["contact_height"] is not None
            ]
            if len(contact_heights) >= 2:
                target_drop = max(0.0, max(contact_heights) - min(contact_heights))
                target_drop = min(target_drop, self.pelvis_max_drop)
            else:
                target_drop = 0.0
            pelvis_drop = min(
                self.pelvis_max_drop, self._smooth_pelvis(target_drop, dt)
            )

            effective_hip_height = hip_height if hip_height > 0.0 else self.leg_length
            pelvis_position = (
                position[0],
                position[1] - pelvis_drop,
                position[2],
            )
            forward = (sin_yaw, 0.0, cos_yaw)

            ankle_offsets = {}
            foot_pitch_deg = {}
            knee_angle_deg = {}
            knee_inverted = {}
            weights = {}
            knees = {}
            surfaces = {}
            locks = {}
            states = {}
            local_positions = {}

            for index, side in enumerate(_SIDES):
                info = feet_info[side]
                side_sign = -1.0 if index == 0 else 1.0
                hip_local_x = side_sign * self.hip_width * 0.5
                hip = (
                    pelvis_position[0] + hip_local_x * cos_yaw,
                    pelvis_position[1] + effective_hip_height,
                    pelvis_position[2] - hip_local_x * sin_yaw,
                )
                weight, angle, inverted, knee = self._solve_leg(
                    hip, info["target"], forward
                )

                ankle_offsets[side] = info["offset"]
                foot_pitch_deg[side] = info["pitch"]
                knee_angle_deg[side] = angle
                knee_inverted[side] = inverted
                weights[side] = weight
                knees[side] = knee
                surfaces[side] = {
                    "accepted": info["accepted"],
                    "normal": info["normal"],
                    "toe": info["toe"],
                    "heel": info["heel"],
                }

                if info["accepted"]:
                    if info["was_locked"]:
                        local = self._local[side]
                    else:
                        local = info["input_local"]
                    locked = True
                    state = "locked"
                    world = self._local_to_world(
                        local, position, cos_yaw, sin_yaw
                    )
                else:
                    local = info["input_local"]
                    locked = False
                    state = "unlocked"
                    world = info["ankle"]

                local_positions[side] = local
                locks[side] = {
                    "locked": locked,
                    "state": state,
                    "local": local,
                    "world": world,
                }
                states[side] = state

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            pelvis_writes = 1
            pelvis = {
                "drop": pelvis_drop,
                "position": pelvis_position,
                "max_drop": self.pelvis_max_drop,
                "writes": pelvis_writes,
            }

            for side in _SIDES:
                self._locked[side] = locks[side]["locked"]
                self._states[side] = states[side]
                self._local[side] = (
                    local_positions[side] if locks[side]["locked"] else None
                )
                self._last_pitch[side] = foot_pitch_deg[side]
                self._pitch_initialized[side] = True
            self._pelvis_drop = pelvis_drop
            self._pelvis_initialized = True
            self._pelvis_writes += pelvis_writes
            self._pelvis_snapshot = pelvis
            self._last_elapsed_ms = elapsed_ms

            return {
                "ankle_offsets": ankle_offsets,
                "foot_offsets": ankle_offsets,
                "offsets": ankle_offsets,
                "foot_pitch_deg": foot_pitch_deg,
                "knee_angle_deg": knee_angle_deg,
                "knee_inverted": knee_inverted,
                "knees": knees,
                "weights": weights,
                "locks": locks,
                "surfaces": surfaces,
                "states": states,
                "pelvis_drop": pelvis_drop,
                "pelvis": pelvis,
                "pelvis_writes": pelvis_writes,
                "elapsed_ms": elapsed_ms,
            }

    def release(self, foot=None, reason="hit"):
        with self._mutex:
            if foot is not None and foot not in _SIDES:
                raise ValueError(f"unknown foot: {foot}")
            if not isinstance(reason, str):
                raise ValueError("reason must be a string")

            targets = _SIDES if foot is None or reason == "death" else (foot,)
            for side in targets:
                self._locked[side] = False
                self._states[side] = "released"
                self._local[side] = None
            self._last_release = {"foot": foot, "reason": reason}

    def stats(self):
        with self._mutex:
            return {
                "locked": dict(self._locked),
                "states": dict(self._states),
                "pelvis_writes": self._pelvis_writes,
                "pelvis_drop": self._pelvis_drop,
                "pelvis": dict(self._pelvis_snapshot),
                "elapsed_ms": self._last_elapsed_ms,
                "last_elapsed_ms": self._last_elapsed_ms,
                "last_release": (
                    dict(self._last_release)
                    if self._last_release is not None
                    else None
                ),
            }
