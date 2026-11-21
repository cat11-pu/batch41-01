"""Stereo view planning for VR (initial stub).

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

    def set_resolution(self, width, height):
        raise NotImplementedError

    def submit_pose(self, position=(0.0, 0.0, 0.0), yaw_deg=0.0):
        raise NotImplementedError

    def lock_pose(self):
        raise NotImplementedError

    def plan(self, elapsed_ms=0.0):
        raise NotImplementedError

    def frustum_cull(self, point):
        raise NotImplementedError

    def reproject(self, uv, depth, prev_uv, prev_depth, tolerance=0.1):
        raise NotImplementedError
