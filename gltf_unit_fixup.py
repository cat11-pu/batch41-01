"""glTF unit / axis repair (initial stub).

Public API::

    GltfUnitFixup(target_unit="m", target_up="y", target_fps=30.0)
        .inspect(asset) -> report dict (must not mutate the asset)
        .apply(asset) -> {"asset": fixed_asset, "report": report dict}

Asset shape used by the checks::

    {
        "name": str,
        "asset": {"unit": "m" | "cm" | "mm" | "in" | "ft", "up_axis": "y" | "z"},
        "nodes": [
            {"name": str, "mesh": int | None, "skin": int | None,
             "translation": [x, y, z], "scale": [x, y, z],
             "children": [int, ...], "parent": int | None},
            ...
        ],
        "meshes": [
            {"id": int, "vertices": [[x, y, z], ...], "pivot": [x, y, z]},
            ...
        ],
        "skins": [
            {"id": int, "joints": [int, ...],
             "inverse_bind_matrices": [[[4x4 row-major]], ...]},
            ...
        ],
        "animations": [
            {"name": str, "duration": float, "sample_rate": float,
             "keyframes": [[time, ...], ...]},
            ...
        ],
    }

Behaviour enforced by the checks:

* unit comes from the asset declaration (missing means metres);
* Z-up to Y-up conversion is `(x, y, z) -> (x, z, -y)` and is applied exactly
  once to mesh vertices and node translations;
* if a mesh declares a non-zero pivot, the pivot is baked into the vertices and
  that node's translation is cleared (never both), and the report counts it;
* inverse bind matrix translations follow the same unit scale (divided by the
  vertex scale) while rotation/scale blocks are untouched;
* animation keyframe times and duration are multiplied by
  source_sample_rate / target_fps;
* a mesh referenced by several nodes is fixed once and stays shared;
* every root node is preserved;
* root nodes with no mesh, skin or children (empty nodes) and zero-scale
  (singular) nodes raise ValueError;
* state is local to each `apply` call, so one asset never changes another.
"""
import copy
import time


class GltfUnitFixup:
    def __init__(self, target_unit="m", target_up="y", target_fps=30.0):
        self.target_unit = target_unit
        self.target_up = target_up
        self.target_fps = target_fps

    def inspect(self, asset):
        nodes = asset.get("nodes", [])
        return {
            "unit": asset.get("asset", {}).get("unit", "m"),
            "up_axis": asset.get("asset", {}).get("up_axis", "y"),
            "node_count": len(nodes),
            "mesh_count": len(asset.get("meshes", [])),
            "animation_count": len(asset.get("animations", [])),
            "roots": [i for i, node in enumerate(nodes)
                      if node.get("parent") is None],
        }

    def apply(self, asset):
        start = time.perf_counter()
        fixed = copy.deepcopy(asset)

        declared = fixed.get("asset", {})
        unit = declared.get("unit", "m")
        up_axis = declared.get("up_axis", "y")
        scale = _UNIT_TO_METERS[unit] / _UNIT_TO_METERS[self.target_unit]
        convert_axis = up_axis == "z" and self.target_up == "y"

        nodes = fixed.get("nodes", [])
        meshes = fixed.get("meshes", [])
        skins = fixed.get("skins", [])
        animations = fixed.get("animations", [])

        self._validate_nodes(nodes)

        fixups = []
        mesh_fixups = 0
        pivot_baked = 0
        pivoted_mesh_ids = set()

        for mesh in meshes:
            changed = False
            pivot = mesh.get("pivot") or [0.0, 0.0, 0.0]
            if any(component != 0.0 for component in pivot):
                mesh["vertices"] = [
                    [vertex[0] - pivot[0], vertex[1] - pivot[1],
                     vertex[2] - pivot[2]]
                    for vertex in mesh["vertices"]
                ]
                mesh["pivot"] = [0.0, 0.0, 0.0]
                pivoted_mesh_ids.add(mesh.get("id"))
                pivot_baked += 1
                changed = True
            if convert_axis:
                mesh["vertices"] = [
                    [vertex[0], vertex[2], -vertex[1]]
                    for vertex in mesh["vertices"]
                ]
                changed = True
            if scale != 1.0:
                mesh["vertices"] = [
                    [vertex[0] * scale, vertex[1] * scale,
                     vertex[2] * scale]
                    for vertex in mesh["vertices"]
                ]
                changed = True
            if changed:
                mesh_fixups += 1

        if pivoted_mesh_ids:
            for node in nodes:
                if node.get("mesh") in pivoted_mesh_ids:
                    node["translation"] = [0.0, 0.0, 0.0]

        if convert_axis or scale != 1.0:
            for node in nodes:
                translation = node.get("translation")
                if not translation:
                    continue
                x, y, z = translation
                if convert_axis:
                    x, y, z = x, z, -y
                node["translation"] = [x * scale, y * scale, z * scale]

        if scale != 1.0:
            for skin in skins:
                for matrix in skin.get("inverse_bind_matrices", []):
                    matrix[0][3] /= scale
                    matrix[1][3] /= scale
                    matrix[2][3] /= scale
            if skins:
                fixups.append("inverse_bind_matrices_rescaled")

        animation_fixups = 0
        for animation in animations:
            sample_rate = animation.get("sample_rate", self.target_fps)
            factor = sample_rate / self.target_fps
            if factor == 1.0:
                continue
            animation["duration"] = animation.get("duration", 0.0) * factor
            animation["keyframes"] = [
                [keyframe[0] * factor] + list(keyframe[1:])
                for keyframe in animation.get("keyframes", [])
            ]
            animation["sample_rate"] = self.target_fps
            animation_fixups += 1

        if convert_axis:
            fixups.append("axis_z_up_to_y_up")
        if scale != 1.0:
            fixups.append("unit_scaled")
        if pivot_baked:
            fixups.append("pivot_baked")
        if animation_fixups:
            fixups.append("animation_resampled")

        report = {
            "unit": unit,
            "up_axis": up_axis,
            "node_count": len(nodes),
            "mesh_count": len(meshes),
            "animation_count": len(animations),
            "roots": [i for i, node in enumerate(nodes)
                      if node.get("parent") is None],
            "mesh_fixups": mesh_fixups,
            "animation_fixups": animation_fixups,
            "pivot_baked": pivot_baked,
            "fixups": fixups,
            "elapsed_ms": (time.perf_counter() - start) * 1000.0,
        }
        return {"asset": fixed, "report": report}

    @staticmethod
    def _validate_nodes(nodes):
        for node in nodes:
            scale = node.get("scale") or [1.0, 1.0, 1.0]
            if any(component == 0.0 for component in scale):
                raise ValueError(
                    "node %r has a singular (zero) scale" % node.get("name"))
            if (node.get("parent") is None
                    and node.get("mesh") is None
                    and node.get("skin") is None
                    and not node.get("children")):
                raise ValueError(
                    "root node %r is empty (no mesh, skin or children)"
                    % node.get("name"))


_UNIT_TO_METERS = {
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
    "in": 0.0254,
    "ft": 0.3048,
}
