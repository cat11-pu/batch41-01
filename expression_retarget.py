"""Semantic expression retargeting between facial rigs."""


class ExpressionRetarget:
    def __init__(self, source_shapes, target_shapes, mapping=None, mirrored=False):
        self.source_shapes = list(source_shapes)
        self.target_shapes = list(target_shapes)
        self.mapping = dict(mapping or {})
        self.mirrored = mirrored
        self.last = {}

    def retarget(self, frame):
        raise NotImplementedError

    def retarget_at(self, frame, time_ms, next_frame, next_time_ms, at_ms):
        raise NotImplementedError

    def blend(self, expression, viseme, mode="normalized"):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
