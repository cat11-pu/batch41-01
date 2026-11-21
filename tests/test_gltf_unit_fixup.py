import copy

import pytest

import gltf_unit_fixup as guf


def root_node(mesh=0, translation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0),
              parent=None, children=None, name="root", skin=None):
    return {
        "name": name,
        "mesh": mesh,
        "skin": skin,
        "translation": list(translation),
        "scale": list(scale),
        "children": list(children or []),
        "parent": parent,
    }


def mesh(vertices=None, pivot=(0.0, 0.0, 0.0)):
    return {
        "id": 0,
        "vertices": [list(vertex) for vertex in (vertices or [[100.0, 200.0, 300.0]])],
        "pivot": list(pivot),
    }


def make_asset(unit="cm", up_axis="y", vertices=None, nodes=None, meshes=None,
               skins=None, animations=None):
    payload = {
        "name": "test_asset",
        "asset": {"up_axis": up_axis},
        "nodes": nodes if nodes is not None else [root_node()],
        "meshes": meshes if meshes is not None else [mesh(vertices)],
        "skins": list(skins or []),
        "animations": list(animations or []),
    }
    if unit is not None:
        payload["asset"]["unit"] = unit
    return payload


def identity_matrix():
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def test_inspect_reports_asset_metadata():
    asset = make_asset(unit="cm", up_axis="z")
    report = guf.GltfUnitFixup().inspect(asset)
    assert report["unit"] == "cm"
    assert report["up_axis"] == "z"
    assert report["node_count"] == 1
    assert report["mesh_count"] == 1
    assert report["animation_count"] == 0


def test_inspect_does_not_mutate_asset():
    asset = make_asset(unit="cm", up_axis="z")
    before = copy.deepcopy(asset)
    guf.GltfUnitFixup().inspect(asset)
    assert asset == before


def test_apply_scales_centimeters_to_meters():
    asset = make_asset(unit="cm", up_axis="y", vertices=[[100.0, 200.0, 300.0]],
                       nodes=[root_node(translation=(100.0, 0.0, 0.0))])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([1.0, 2.0, 3.0])
    assert fixed["nodes"][0]["translation"] == pytest.approx([1.0, 0.0, 0.0])


def test_apply_scales_millimeters_to_meters():
    asset = make_asset(unit="mm", up_axis="y", vertices=[[1000.0, 500.0, 0.0]])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([1.0, 0.5, 0.0])


def test_axis_conversion_is_applied_once():
    asset = make_asset(unit="cm", up_axis="z", vertices=[[100.0, 200.0, 300.0]],
                       nodes=[root_node(translation=(0.0, 0.0, 100.0))])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([1.0, 3.0, -2.0])
    assert fixed["nodes"][0]["translation"] == pytest.approx([0.0, 1.0, 0.0])


def test_y_up_asset_is_not_rotated():
    asset = make_asset(unit="m", up_axis="y", vertices=[[1.0, 2.0, 3.0]])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([1.0, 2.0, 3.0])


def test_inverse_bind_matrices_follow_unit_scale():
    matrix = identity_matrix()
    matrix[0][3] = 1.0
    matrix[1][3] = 2.0
    matrix[2][3] = 3.0
    skin = {"id": 0, "joints": [0], "inverse_bind_matrices": [matrix]}
    asset = make_asset(unit="cm", up_axis="y", skins=[skin],
                       nodes=[root_node(skin=0)])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    bind = fixed["skins"][0]["inverse_bind_matrices"][0]
    assert bind[0][3] == pytest.approx(100.0)
    assert bind[1][3] == pytest.approx(200.0)
    assert bind[2][3] == pytest.approx(300.0)
    assert bind[0][0] == pytest.approx(1.0)


def test_animation_times_are_rescaled_from_24_to_30():
    animation = {
        "name": "idle",
        "duration": 1.0,
        "sample_rate": 24.0,
        "keyframes": [[0.0, 0.0], [0.5, 1.0], [1.0, 2.0]],
    }
    asset = make_asset(animations=[animation])
    fixed = guf.GltfUnitFixup().apply(asset)["asset"]
    result = fixed["animations"][0]
    assert result["duration"] == pytest.approx(0.8)
    assert [keyframe[0] for keyframe in result["keyframes"]] == pytest.approx([0.0, 0.4, 0.8])


def test_shared_mesh_is_fixed_once_and_stays_shared():
    nodes = [root_node(), root_node(name="second", translation=(200.0, 0.0, 0.0))]
    asset = make_asset(unit="cm", up_axis="y", vertices=[[100.0, 0.0, 0.0]],
                       nodes=nodes)
    result = guf.GltfUnitFixup().apply(asset)
    fixed = result["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([1.0, 0.0, 0.0])
    assert fixed["nodes"][0]["mesh"] == 0
    assert fixed["nodes"][1]["mesh"] == 0
    assert result["report"]["mesh_fixups"] == 1


def test_multiple_roots_are_preserved():
    nodes = [
        root_node(children=[2]),
        root_node(name="second"),
        root_node(mesh=None, name="child", parent=0),
    ]
    asset = make_asset(nodes=nodes)
    result = guf.GltfUnitFixup().apply(asset)
    assert result["report"]["node_count"] == 3
    assert result["report"]["roots"] == [0, 1]
    assert len(result["asset"]["nodes"]) == 3


def test_baked_pivot_is_not_applied_twice():
    asset = make_asset(unit="cm", up_axis="y", vertices=[[0.0, 0.0, 100.0]],
                       meshes=[mesh([[0.0, 0.0, 100.0]], pivot=(0.0, 0.0, 100.0))],
                       nodes=[root_node(translation=(5.0, 0.0, 0.0))])
    result = guf.GltfUnitFixup().apply(asset)
    fixed = result["asset"]
    assert fixed["meshes"][0]["vertices"][0] == pytest.approx([0.0, 0.0, 0.0])
    assert fixed["nodes"][0]["translation"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["report"]["pivot_baked"] == 1


def test_empty_node_is_rejected():
    nodes = [{"name": "empty", "mesh": None, "skin": None,
              "translation": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0],
              "children": [], "parent": None}]
    asset = make_asset(nodes=nodes, meshes=[])
    with pytest.raises(ValueError):
        guf.GltfUnitFixup().apply(asset)


def test_singular_scale_is_rejected():
    asset = make_asset(nodes=[root_node(scale=(0.0, 1.0, 1.0))])
    with pytest.raises(ValueError):
        guf.GltfUnitFixup().apply(asset)


def test_import_state_is_isolated_per_asset():
    fixup = guf.GltfUnitFixup()
    first = fixup.apply(make_asset(unit="cm", vertices=[[100.0, 0.0, 0.0]]))
    second = fixup.apply(make_asset(unit=None, vertices=[[100.0, 0.0, 0.0]]))
    assert first["asset"]["meshes"][0]["vertices"][0] == pytest.approx([1.0, 0.0, 0.0])
    assert second["asset"]["meshes"][0]["vertices"][0] == pytest.approx([100.0, 0.0, 0.0])


def test_report_contains_counts_and_elapsed_time():
    animation = {"name": "idle", "duration": 1.0, "sample_rate": 24.0,
                 "keyframes": [[0.0, 0.0]]}
    asset = make_asset(animations=[animation])
    report = guf.GltfUnitFixup().apply(asset)["report"]
    assert report["node_count"] == 1
    assert report["mesh_fixups"] == 1
    assert report["animation_fixups"] == 1
    assert report["pivot_baked"] == 0
    assert report["roots"] == [0]
    assert report["elapsed_ms"] >= 0.0
    assert isinstance(report["fixups"], list)
