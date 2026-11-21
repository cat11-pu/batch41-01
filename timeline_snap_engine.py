"""Frame accurate timeline snapping, moving, rippling and undo."""


class TimelineSnapEngine:
    def __init__(self, fps_num=30, fps_den=1):
        self.fps_num = fps_num
        self.fps_den = fps_den
        self.tracks = {}
        self.clips = {}
        self.groups = {}
        self.history = []
        self._edit_owner = None

    def add_track(self, track_id, kind="video", locked=False, linked=None):
        raise NotImplementedError

    def add_clip(self, clip_id, track_id, start, duration, transition=0):
        raise NotImplementedError

    def clip_state(self, clip_id):
        raise NotImplementedError

    def add_group(self, group_id, clip_ids):
        raise NotImplementedError

    def collapse_group(self, group_id, collapsed=True):
        raise NotImplementedError

    def move_group(self, group_id, start, owner=None):
        raise NotImplementedError

    def snap(self, track_id, frame, tolerance=2, exclude=None):
        raise NotImplementedError

    def move(self, clip_id, start, owner=None):
        raise NotImplementedError

    def move_many(self, clip_ids, start, owner=None):
        raise NotImplementedError

    def ripple_delete(self, clip_id, owner=None):
        raise NotImplementedError

    def overlap_intervals(self):
        raise NotImplementedError

    def undo(self):
        raise NotImplementedError

    def pixels_to_frames(self, pixels, zoom_num, zoom_den=1):
        raise NotImplementedError

    def begin_edit(self, owner):
        raise NotImplementedError

    def end_edit(self, owner):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
