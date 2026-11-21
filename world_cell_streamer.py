"""World cell streaming with priorities, refcounts and main-thread callbacks."""


class WorldCellStreamer:
    def __init__(self, cell_size=64.0, load_radius=2, hysteresis=1):
        self.cell_size = cell_size
        self.load_radius = load_radius
        self.hysteresis = hysteresis
        self.player = (0.0, 0.0)
        self.heading_deg = 0.0
        self.refs = {}
        self.resident = set()
        self.pending = {}
        self.empty = set()
        self.callbacks = []
        self.main_queue = []

    def cell_at(self, x, z):
        raise NotImplementedError

    def set_player(self, position, heading_deg=0.0):
        raise NotImplementedError

    def request(self, cell, owner="system"):
        raise NotImplementedError

    def release(self, cell, owner="system"):
        raise NotImplementedError

    def add_dependency(self, cell, dependency):
        raise NotImplementedError

    def mark_empty(self, cell):
        raise NotImplementedError

    def register_callback(self, callback):
        raise NotImplementedError

    def complete(self, cell, thread="io"):
        raise NotImplementedError

    def pump_main_thread(self):
        raise NotImplementedError

    def teleport(self, position):
        raise NotImplementedError

    def update(self, budget_ms=2.0):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
