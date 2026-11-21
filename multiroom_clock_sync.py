"""Clock offset, drift and playback alignment for multi-room audio."""


class MultiroomClockSync:
    def __init__(self, master="living", target_buffer_ms=200.0):
        self.master = master
        self.target_buffer_ms = target_buffer_ms
        self.devices = {}
        self._playing = {}
        self._paused_at = {}

    def register(self, device_id, latency_ms=0.0, sample_rate=48000, bluetooth=False):
        raise NotImplementedError

    def add_sample(self, device_id, local_ms, master_ms, rtt_ms):
        raise NotImplementedError

    def estimate(self, device_id):
        raise NotImplementedError

    def start(self, device_id, master_ms=0.0):
        raise NotImplementedError

    def position(self, device_id, master_ms):
        raise NotImplementedError

    def render_position(self, device_id, master_ms):
        raise NotImplementedError

    def pause(self, device_id, master_ms):
        raise NotImplementedError

    def resume(self, device_id, master_ms):
        raise NotImplementedError

    def join(self, device_id):
        raise NotImplementedError

    def resample_ratio(self, device_id):
        raise NotImplementedError

    def catchup_rate(self, device_id, buffer_ms):
        raise NotImplementedError

    def migrate_master(self, new_master, master_ms):
        raise NotImplementedError

    def correct_clock(self, device_id, measured_offset_ms):
        raise NotImplementedError

    def sync_round(self, reports, budget_ms=5.0):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
