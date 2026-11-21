import pytest

import caption_retime as cr


def cap(cid, start, duration, forced=False):
    return {"id": cid, "start": start, "duration": duration, "forced": forced}


def test_retime_uses_23976_not_24():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 24000, 24000)], source_fps=(24000, 1001), target_fps=(25, 1))
    assert result["captions"][0]["start"] == 25025


def test_ninety_minutes_does_not_drift():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 129600, 1)], source_fps=(24000, 1001), target_fps=(25, 1))
    assert result["captions"][0]["start"] == 135135


def test_drop_frame_timecode():
    r = cr.CaptionRetime()
    assert r.timecode_to_frame("00:01:00;00", fps=(30000, 1001), drop_frame=True) == 1798
    assert r.timecode_to_frame("00:01:00:00", fps=(30000, 1001), drop_frame=False) == 1800


def test_rounding_does_not_accumulate_per_caption():
    r = cr.CaptionRetime()
    captions = [cap(i, i * 24000, 1000) for i in range(100)]
    result = r.retime(captions, source_fps=(24000, 1001), target_fps=(25, 1))
    assert result["captions"][99]["start"] == 2477475


def test_duration_is_scaled_with_rate():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 0, 24000)], source_fps=(24000, 1001), target_fps=(25, 1))
    assert result["captions"][0]["duration"] == 25025


def test_minimum_display_duration_is_kept():
    r = cr.CaptionRetime(min_duration_frames=2)
    result = r.retime([cap(1, 0, 1)], source_fps=(24, 1), target_fps=(25, 1))
    assert result["captions"][0]["duration"] == 2


def test_adjacent_captions_keep_non_negative_gap():
    r = cr.CaptionRetime()
    captions = [cap(1, 0, 24000), cap(2, 24000, 24000)]
    result = r.retime(captions, source_fps=(24, 1), target_fps=(25, 1))
    first, second = result["captions"]
    assert first["start"] == 0
    assert second["start"] == 25000
    assert second["start"] - (first["start"] + first["duration"]) >= 0


def test_forced_flag_is_preserved_separately():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 0, 100, True), cap(2, 200, 100, False)],
                      source_fps=(24, 1), target_fps=(25, 1))
    assert result["captions"][0]["forced"] is True
    assert result["captions"][1]["forced"] is False


def test_offset_is_in_target_frames_not_milliseconds():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 0, 10)], source_fps=(24, 1), target_fps=(25, 1),
                      offset_frames=1000)
    assert result["captions"][0]["start"] == 1000


def test_caption_beyond_video_is_clipped_and_counted():
    r = cr.CaptionRetime()
    result = r.retime([cap(1, 900, 240)], source_fps=(24, 1), target_fps=(25, 1),
                      video_frames=1000)
    entry = result["captions"][0]
    assert entry["start"] == 938
    assert entry["duration"] == 62
    assert entry["start"] + entry["duration"] == 1000
    assert result["clipped"] == 1


def test_vfr_segments_use_per_segment_rate():
    r = cr.CaptionRetime()
    segments = [
        {"start": 0, "fps": (24, 1)},
        {"start": 240, "fps": (30, 1)},
    ]
    result = r.retime_vfr([cap(1, 360, 60)], segments, target_fps=(25, 1))
    assert result["captions"][0]["start"] == 350


def test_negative_start_and_zero_duration_are_rejected():
    r = cr.CaptionRetime()
    with pytest.raises(ValueError):
        r.retime([cap(1, -1, 10)], source_fps=(24, 1), target_fps=(25, 1))
    with pytest.raises(ValueError):
        r.retime([cap(1, 0, 0)], source_fps=(24, 1), target_fps=(25, 1))


def test_batch_isolates_projects():
    r = cr.CaptionRetime()
    projects = [
        {"name": "a", "source_fps": (24, 1), "target_fps": (25, 1),
         "captions": [cap(1, 240, 240)]},
        {"name": "b", "source_fps": (30, 1), "target_fps": (25, 1),
         "captions": [cap(1, 240, 240)]},
    ]
    result = r.batch(projects)
    assert result["a"]["captions"][0]["start"] == 250
    assert result["b"]["captions"][0]["start"] == 200


def test_frame_to_timecode_drop_frame_round_trip():
    r = cr.CaptionRetime()
    assert r.frame_to_timecode(1798, fps=(30000, 1001), drop_frame=True) == "00:01:00;00"
    assert r.frame_to_timecode(1800, fps=(30000, 1001), drop_frame=False) == "00:01:00:00"


def test_large_batch_reports_count_and_elapsed():
    r = cr.CaptionRetime()
    captions = [cap(i, i * 24000, 1000) for i in range(1000)]
    result = r.retime(captions, source_fps=(24000, 1001), target_fps=(25, 1))
    assert result["count"] == 1000
    assert result["captions"][-1]["start"] == 24999975
    assert result["elapsed_ms"] >= 0
