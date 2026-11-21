import math

import pytest

import bone_quant_pack as bq


def bone(rot=(1.0, 0.0, 0.0, 0.0), trans=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0)):
    return {"rot": rot, "trans": trans, "scale": scale}


def clip(frames=3, fps=60, duration=0.05, bones=None):
    if bones is None:
        bones = {"spine": bone()}
    rows = []
    for index in range(frames):
        t = duration * index / max(frames - 1, 1)
        rows.append({"t": t, "bones": {name: dict(data) for name, data in bones.items()}})
    return {"id": "walk", "fps": fps, "duration": duration, "frames": rows}


def angle(first, second):
    dot = abs(sum(a * b for a, b in zip(first, second)))
    return 2.0 * math.acos(min(1.0, dot))


def test_quaternion_sign_is_normalized():
    positive = bq.BoneQuantPack()
    negative = bq.BoneQuantPack()
    first = clip(frames=1, duration=0.0, bones={"root": bone(rot=(0.5, 0.5, 0.5, 0.5))})
    second = clip(frames=1, duration=0.0, bones={"root": bone(rot=(-0.5, -0.5, -0.5, -0.5))})
    decoded_first = positive.decode(positive.encode(first))["frames"][0]["bones"]["root"]["rot"]
    decoded_second = negative.decode(negative.encode(second))["frames"][0]["bones"]["root"]["rot"]
    assert decoded_first == pytest.approx(decoded_second)


def test_rotation_error_is_bounded():
    rotations = [
        (0.7071068, 0.7071068, 0.0, 0.0),
        (0.9238795, 0.0, 0.3826834, 0.0),
        (0.5, 0.5, 0.5, 0.5),
        (0.2588190, 0.0, 0.0, 0.9659258),
    ]
    packer = bq.BoneQuantPack()
    for rot in rotations:
        source = clip(frames=1, duration=0.0, bones={"root": bone(rot=rot)})
        decoded = packer.decode(packer.encode(source))["frames"][0]["bones"]["root"]["rot"]
        assert angle(rot, decoded) < math.radians(2.0)


def test_component_index_fits_two_bits():
    packer = bq.BoneQuantPack()
    rotations = [(0.999, 0.01, 0.02, 0.03), (0.1, 0.9, 0.2, 0.3), (0.2, 0.1, 0.95, 0.2)]
    frames = []
    for index, rot in enumerate(rotations):
        frames.append({"t": float(index), "bones": {"root": bone(rot=rot)}})
    source = {"id": "fingers", "fps": 30, "duration": 2.0, "frames": frames}
    packed = packer.encode(source)
    assert len(packed["frames"]) == 3
    for frame in packed["frames"]:
        assert 0 <= frame["bones"]["root"]["index"] <= 3


def test_all_bones_survive_decode():
    packer = bq.BoneQuantPack()
    bones = {
        "root": bone(),
        "finger_index_01": bone(rot=(0.7071068, 0.0, 0.7071068, 0.0)),
        "tail": bone(scale=(1.1, 0.9, 1.0)),
    }
    source = clip(frames=2, bones=bones)
    decoded = packer.decode(packer.encode(source))
    assert set(decoded["frames"][0]["bones"]) == set(bones)


def test_per_bone_translation_range_is_used():
    packer = bq.BoneQuantPack()
    packer.set_bone_range("hand", -0.2, 0.2)
    packer.set_bone_range("foot", -2.0, 2.0)
    bones = {
        "hand": bone(trans=(0.19, -0.19, 0.0)),
        "foot": bone(trans=(1.95, -1.95, 0.0)),
    }
    source = clip(frames=1, duration=0.0, bones=bones)
    decoded = packer.decode(packer.encode(source))["frames"][0]["bones"]
    assert decoded["hand"]["trans"] == pytest.approx((0.19, -0.19, 0.0), abs=0.01)
    assert decoded["foot"]["trans"] == pytest.approx((1.95, -1.95, 0.0), abs=0.05)


def test_translation_error_stays_within_step():
    packer = bq.BoneQuantPack()
    packer.set_bone_range("root", -1.0, 1.0)
    source = clip(frames=1, duration=0.0, bones={"root": bone(trans=(0.3333, -0.7777, 0.1234))})
    decoded = packer.decode(packer.encode(source))["frames"][0]["bones"]["root"]["trans"]
    step = 2.0 / 255.0
    for original, result in zip((0.3333, -0.7777, 0.1234), decoded):
        assert abs(original - result) <= step


def test_scale_channel_is_preserved():
    packer = bq.BoneQuantPack()
    source = clip(frames=1, duration=0.0, bones={"tail": bone(scale=(1.2, 0.9, 1.0))})
    decoded = packer.decode(packer.encode(source))["frames"][0]["bones"]["tail"]["scale"]
    assert decoded == pytest.approx((1.2, 0.9, 1.0), rel=0.02)


def test_invalid_bone_range_is_rejected():
    packer = bq.BoneQuantPack()
    with pytest.raises(ValueError):
        packer.set_bone_range("hand", 1.0, -1.0)
    with pytest.raises(ValueError):
        packer.set_bone_range("hand", 1.0, 1.0)


def test_keyframe_interval_is_configurable():
    fast_motion = clip(frames=20, fps=60, duration=19 / 60.0)
    packed_fast = bq.BoneQuantPack(key_interval=0.1).encode(fast_motion)
    packed_slow = bq.BoneQuantPack(key_interval=0.5).encode(fast_motion)
    keys_fast = sum(1 for frame in packed_fast["frames"] if frame["key"])
    keys_slow = sum(1 for frame in packed_slow["frames"] if frame["key"])
    assert keys_fast == 4
    assert keys_slow == 1


def test_frames_between_keyframes_are_delta():
    packer = bq.BoneQuantPack(key_interval=0.1)
    frames = packer.encode(clip(frames=20, fps=60, duration=19 / 60.0))["frames"]
    flags = [frame["key"] for frame in frames[:8]]
    assert flags == [True, False, False, False, False, False, True, False]


def test_fast_motion_error_is_bounded():
    packer = bq.BoneQuantPack(key_interval=0.1)
    frames = []
    for index in range(10):
        theta = math.radians(25.0 * index)
        rot = (math.cos(theta / 2.0), 0.0, 0.0, math.sin(theta / 2.0))
        frames.append({"t": index / 60.0, "bones": {"root": bone(rot=rot)}})
    source = {"id": "climb", "fps": 60, "duration": 9 / 60.0, "frames": frames}
    decoded = packer.decode(packer.encode(source))
    for original, result in zip(frames, decoded["frames"]):
        assert angle(original["bones"]["root"]["rot"], result["bones"]["root"]["rot"]) < math.radians(3.0)


def test_non_finite_values_are_rejected():
    packer = bq.BoneQuantPack()
    bad_rot = clip(frames=1, duration=0.0, bones={"root": bone(rot=(float("nan"), 0.0, 0.0, 1.0))})
    with pytest.raises(ValueError):
        packer.encode(bad_rot)
    bad_trans = clip(frames=1, duration=0.0, bones={"root": bone(trans=(float("inf"), 0.0, 0.0))})
    with pytest.raises(ValueError):
        packer.encode(bad_trans)


def test_duration_matches_source():
    packer = bq.BoneQuantPack()
    source = clip(frames=4, fps=30, duration=0.3)
    packed = packer.encode(source)
    decoded = packer.decode(packed)
    assert packed["duration"] == pytest.approx(0.3)
    assert decoded["duration"] == pytest.approx(0.3)
    assert len(decoded["frames"]) == len(source["frames"])


def test_repeat_encode_is_identical():
    packer = bq.BoneQuantPack()
    source = clip(frames=5, fps=60, duration=4 / 60.0)
    assert packer.encode(source) == packer.encode(source)


def test_batch_of_two_hundred_clips_fits_budget():
    packer = bq.BoneQuantPack()
    clips = [clip(frames=1, duration=0.0) for _ in range(200)]
    result = packer.encode_batch(clips, budget_ms=1000.0)
    assert result["encoded"] == 200
    assert result["estimated_ms"] == pytest.approx(100.0)
    assert result["within_budget"] is True
    with pytest.raises(ValueError):
        packer.encode_batch(clips + clips, budget_ms=1000.0)
