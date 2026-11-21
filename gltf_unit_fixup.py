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


class GltfUnitFixup:
    def __init__(self, target_unit="m", target_up="y", target_fps=30.0):
        self.target_unit = target_unit
        self.target_up = target_up
        self.target_fps = target_fps

    def inspect(self, asset):
        raise NotImplementedError

    def apply(self, asset):
        raise NotImplementedError
