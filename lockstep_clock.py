class LockstepClock:
    """Fixed-step logical clock with server-authoritative time."""

    def __init__(self, step_ms=50, max_catch_up=4, frame_space=65536):
        self.step_ms = step_ms
        self.max_catch_up = max_catch_up
        self.frame_space = frame_space
        self.frame = 0

    def current_frame(self):
        raise NotImplementedError

    def advance(self, server_ms, client_ms=None):
        raise NotImplementedError

    def catch_up(self, target_frame, budget=None):
        raise NotImplementedError

    def pause(self, now_ms):
        raise NotImplementedError

    def resume(self, now_ms):
        raise NotImplementedError

    def subscribe(self, client_id, spectator=False):
        raise NotImplementedError

    def broadcast(self):
        raise NotImplementedError

    def unpack(self, packets):
        raise NotImplementedError

    def reconnect(self, client_id, last_frame):
        raise NotImplementedError

    def set_presentation_rate(self, client_id, fps):
        raise NotImplementedError

    def presentation_rate(self, client_id):
        raise NotImplementedError

    def record_input(self, frame, player, action):
        raise NotImplementedError

    def replay(self):
        raise NotImplementedError

    def ack(self, client_id, seq):
        raise NotImplementedError

    def pending(self, client_id):
        raise NotImplementedError

    def compare_frame(self, a, b):
        raise NotImplementedError
