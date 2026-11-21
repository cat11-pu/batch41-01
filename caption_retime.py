"""Retiming of subtitle captions between rational frame rates."""


class CaptionRetime:
    def __init__(self, min_duration_frames=2):
        self.min_duration_frames = min_duration_frames

    def retime(self, captions, source_fps=(24000, 1001), target_fps=(25, 1),
               video_frames=None, offset_frames=0):
        raise NotImplementedError

    def retime_vfr(self, captions, segments, target_fps=(25, 1), video_frames=None):
        raise NotImplementedError

    def timecode_to_frame(self, timecode, fps=(30000, 1001), drop_frame=False):
        raise NotImplementedError

    def frame_to_timecode(self, frame, fps=(30000, 1001), drop_frame=False):
        raise NotImplementedError

    def batch(self, projects):
        raise NotImplementedError
