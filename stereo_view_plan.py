"""Stereo view planning for VR.

Public API::

    StereoViewPlan(width=1920, height=1080, ipd=0.064, near=0.1, far=1000.0,
                   fov_deg=90.0, budget_ms=11.0, default_ipd=0.064)
        .set_resolution(width, height)
        .submit_pose(position=(0, 0, 0), yaw_deg=0.0)
        .lock_pose()
        .plan(elapsed_ms=0.0) -> dict
        .frustum_cull(point) -> bool
        .reproject(uv, depth, prev_uv, prev_depth, tolerance=0.1) -> dict

Conventions: world +x right, +y up, -z forward.  Matrices are row-major 4x4
lists, `matrix[row][column]`.  The projection's horizontal lens shift lives in
`projection[0][2]` and is mirrored between the eyes; the depth mapping in row 2
is identical for both eyes.

`plan` returns::

    {
        "views": [
            {"eye": "left", "position": (x, y, z), "view": 4x4,
             "projection": 4x4, "viewport": (x0, y0, w, h),
             "near": float, "far": float},
            {"eye": "right", ...},
        ],
        "instanced_order": ["left", "right"],
        "pose_used": {"position": (x, y, z), "yaw_deg": float},
        "ipd_used": float,
        "ipd_fallback": bool,
        "shadow_offsets": [(dx, dy, dz), (dx, dy, dz)],
        "degraded": bool,
        "target_scale": float,
        "elapsed_ms": float,
    }

Behaviour enforced by the checks:

* each eye has its own view/projection, offset by +/- ipd/2 around the head;
* single-pass instancing always uses the fixed order left then right;
* culling tests the union of both eye frusta, so geometry visible only to one
  eye survives;
* the pose is only read after `lock_pose`; an unlocked submission is ignored;
* reprojection rejects samples whose current depth is behind the previous
  depth by more than the tolerance;
* zero or negative IPD falls back to `default_ipd` and reports it;
* resolution changes rebuild both viewports;
* shadow offsets are mirrored per eye;
* over budget returns degraded with a target scale below 1, never a silent
  resolution drop;
* returned matrices are copies, so callers cannot mutate planner state.
"""

import math
import threading


def _copy_matrix(matrix):
    return [row[:] for row in matrix]


def _yaw_rotation(yaw_deg):
    angle = math.radians(yaw_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return [
        [cosine, 0.0, sine, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [-sine, 0.0, cosine, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mat_vec(matrix, vector):
    x, y, z, w = vector
    return tuple(
        sum(matrix[row][col] * component for col, component in enumerate((x, y, z, w)))
        for row in range(4)
    )


class StereoViewPlan:
    def __init__(self, width=1920, height=1080, ipd=0.064, near=0.1, far=1000.0,
                 fov_deg=90.0, budget_ms=11.0, default_ipd=0.064):
        self.width = width
        self.height = height
        self.ipd = ipd
        self.near = near
        self.far = far
        self.fov_deg = fov_deg
        self.budget_ms = budget_ms
        self.default_ipd = default_ipd
        self._pose_lock = threading.RLock()
        self._pending_pose = None
        self._locked_pose = ((0.0, 0.0, 0.0), 0.0)

    def set_resolution(self, width, height):
        with self._pose_lock:
            self.width = int(width)
            self.height = int(height)
        return self

    def submit_pose(self, position=(0.0, 0.0, 0.0), yaw_deg=0.0):
        pose = (tuple(float(component) for component in position), float(yaw_deg))
        with self._pose_lock:
            self._pending_pose = pose
        return self

    def lock_pose(self):
        with self._pose_lock:
            if self._pending_pose is not None:
                self._locked_pose = self._pending_pose
                self._pending_pose = None
        return self

    def _effective_ipd(self):
        fallback = not math.isfinite(self.ipd) or self.ipd <= 0.0
        return (self.default_ipd if fallback else self.ipd), fallback

    def _current_pose(self):
        with self._pose_lock:
            position, yaw_deg = self._locked_pose
            return tuple(position), yaw_deg

    def _view_matrix(self, eye_position, yaw_rotation):
        translation = [
            [1.0, 0.0, 0.0, -eye_position[0]],
            [0.0, 1.0, 0.0, -eye_position[1]],
            [0.0, 0.0, 1.0, -eye_position[2]],
            [0.0, 0.0, 0.0, 1.0],
        ]
        return [
            [
                sum(yaw_rotation[row][k] * translation[k][col] for k in range(4))
                for col in range(4)
            ]
            for row in range(4)
        ]

    def _projection_matrix(self, eye_sign, half_ipd):
        aspect = self.width / float(self.height)
        tan_half_fov = math.tan(math.radians(self.fov_deg) * 0.5)
        depth_sum = self.near + self.far
        depth_diff = self.near - self.far
        lens_shift = eye_sign * half_ipd / (self.near * aspect * tan_half_fov)
        return [
            [1.0 / (aspect * tan_half_fov), 0.0, lens_shift, 0.0],
            [0.0, 1.0 / tan_half_fov, 0.0, 0.0],
            [0.0, 0.0, depth_sum / depth_diff,
             2.0 * self.far * self.near / depth_diff],
            [0.0, 0.0, -1.0, 0.0],
        ]

    def plan(self, elapsed_ms=0.0):
        position, yaw_deg = self._current_pose()
        ipd_used, ipd_fallback = self._effective_ipd()
        half_ipd = ipd_used * 0.5
        yaw_rotation = _yaw_rotation(yaw_deg)

        per_eye_width = self.width // 2
        eye_specs = (
            ("left", -1.0, (0, 0, per_eye_width, self.height)),
            ("right", 1.0, (per_eye_width, 0, self.width - per_eye_width,
                            self.height)),
        )

        views = []
        shadow_offsets = []
        for eye_name, eye_sign, viewport in eye_specs:
            offset = _mat_vec(yaw_rotation, (eye_sign * half_ipd, 0.0, 0.0, 1.0))
            eye_position = (
                position[0] + offset[0],
                position[1] + offset[1],
                position[2] + offset[2],
            )
            views.append({
                "eye": eye_name,
                "position": eye_position,
                "view": self._view_matrix(eye_position, yaw_rotation),
                "projection": self._projection_matrix(eye_sign, half_ipd),
                "viewport": tuple(int(component) for component in viewport),
                "near": float(self.near),
                "far": float(self.far),
            })
            shadow_offsets.append((eye_sign * half_ipd, 0.0, 0.0))

        degraded = elapsed_ms > self.budget_ms
        if degraded:
            target_scale = max(0.5, self.budget_ms / float(elapsed_ms))
        else:
            target_scale = 1.0

        return {
            "views": views,
            "instanced_order": ["left", "right"],
            "pose_used": {"position": tuple(position), "yaw_deg": float(yaw_deg)},
            "ipd_used": float(ipd_used),
            "ipd_fallback": ipd_fallback,
            "shadow_offsets": shadow_offsets,
            "degraded": degraded,
            "target_scale": float(target_scale),
            "elapsed_ms": float(elapsed_ms),
        }

    def frustum_cull(self, point):
        position, yaw_deg = self._current_pose()
        ipd_used, _ = self._effective_ipd()
        half_ipd = ipd_used * 0.5
        yaw_rotation = _yaw_rotation(yaw_deg)
        aspect = self.width / float(self.height)
        tan_half_fov = math.tan(math.radians(self.fov_deg) * 0.5)
        homogeneous = (point[0], point[1], point[2], 1.0)

        for eye_sign in (-1.0, 1.0):
            offset = _mat_vec(yaw_rotation, (eye_sign * half_ipd, 0.0, 0.0, 1.0))
            eye_position = (
                position[0] + offset[0],
                position[1] + offset[1],
                position[2] + offset[2],
            )
            translated = (
                homogeneous[0] - eye_position[0],
                homogeneous[1] - eye_position[1],
                homogeneous[2] - eye_position[2],
                1.0,
            )
            view_point = _mat_vec(yaw_rotation, translated)
            depth = -view_point[2]
            if depth < self.near or depth > self.far:
                continue
            if abs(view_point[0]) > aspect * tan_half_fov * depth:
                continue
            if abs(view_point[1]) > tan_half_fov * depth:
                continue
            return True
        return False

    def reproject(self, uv, depth, prev_uv, prev_depth, tolerance=0.1):
        depth_delta = float(depth) - float(prev_depth)
        occluded = depth_delta > float(tolerance) + 1e-9
        return {
            "valid": not occluded,
            "uv": tuple(float(component) for component in uv),
            "prev_uv": tuple(float(component) for component in prev_uv),
            "depth": float(depth),
            "prev_depth": float(prev_depth),
            "depth_delta": depth_delta,
            "tolerance": float(tolerance),
            "occluded": occluded,
        }
