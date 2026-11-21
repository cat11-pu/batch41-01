"""Multi-source live compositor with a unified master clock."""


class MultiviewCompositor:
    def __init__(self, width=1920, height=1080, budget_ms=8.0):
        self.width = width
        self.height = height
        self.budget_ms = budget_ms
        self.layout = []
        self.frames = {}
        self.queue_swaps = 0

    def set_layout(self, cells):
        raise NotImplementedError

    def push_frame(self, source, pts, timebase=(1000, 1), duration_ms=None,
                   transfer="bt709"):
        raise NotImplementedError

    def select_frame(self, source, master_ms):
        raise NotImplementedError

    def compose(self, master_ms, budget_ms=None):
        raise NotImplementedError

    def align_audio(self, master_ms, tracks):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
