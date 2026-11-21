"""Clock offset, drift and playback alignment for multi-room audio."""

import copy
import threading
import time


class MultiroomClockSync:
    REFERENCE_RATE = 48000.0
    MAX_CATCHUP_RATE = 1.05
    MAX_CORRECTION_STEP_MS = 5.0
    DEFAULT_INTERVAL_MS = 100.0
    MAX_INTERVAL_MS = 1000.0
    # Lower bound of per-report processing cost used by the scheduler when
    # deciding whether a sync round fits its CPU budget.
    MIN_COST_PER_REPORT_MS = 0.001

    def __init__(self, master="living", target_buffer_ms=200.0):
        self.master = master
        self.target_buffer_ms = target_buffer_ms
        self.devices = {}
        self._playing = {}
        self._paused_at = {}
        self.interval_ms = self.DEFAULT_INTERVAL_MS
        self.over_budget_events = []
        self._lock = threading.Lock()

    def register(self, device_id, latency_ms=0.0, sample_rate=48000, bluetooth=False):
        with self._lock:
            self.devices[device_id] = {
                "offset_ms": 0.0,
                "drift_ppm": 0.0,
                "latency_ms": float(latency_ms),
                "sample_rate": sample_rate,
                "bluetooth": bool(bluetooth),
                "buffer_ms": 0.0,
                "pending_correction_ms": 0.0,
                "samples": [],
            }
        return device_id

    def _device(self, device_id):
        try:
            return self.devices[device_id]
        except KeyError:
            raise KeyError("unknown device: %r" % (device_id,))

    def add_sample(self, device_id, local_ms, master_ms, rtt_ms):
        if rtt_ms < 0:
            raise ValueError("rtt_ms must be non-negative")
        with self._lock:
            dev = self._device(device_id)
            samples = dev["samples"]
            if samples and local_ms <= samples[-1]["local_ms"]:
                return False
            samples.append(
                {
                    "local_ms": float(local_ms),
                    "master_ms": float(master_ms),
                    "rtt_ms": float(rtt_ms),
                    "offset_ms": float(local_ms) - float(master_ms),
                }
            )
            best = min(samples, key=lambda s: s["rtt_ms"])
            dev["offset_ms"] = best["offset_ms"]
            if len(samples) >= 2:
                first, last = samples[0], samples[-1]
                span = last["local_ms"] - first["local_ms"]
                if span > 0:
                    dev["drift_ppm"] = (
                        (last["offset_ms"] - first["offset_ms"]) / span * 1e6
                    )
            return True

    def estimate(self, device_id):
        with self._lock:
            dev = self._device(device_id)
            return {
                "offset_ms": dev["offset_ms"],
                "drift_ppm": dev["drift_ppm"],
                "latency_ms": dev["latency_ms"],
                "buffer_ms": dev["buffer_ms"],
                "samples": len(dev["samples"]),
            }

    def start(self, device_id, master_ms=0.0):
        with self._lock:
            self._device(device_id)
            self._playing[device_id] = {
                "base_position_ms": 0.0,
                "base_master_ms": float(master_ms),
                "paused": False,
            }

    def position(self, device_id, master_ms):
        with self._lock:
            self._device(device_id)
            state = self._playing.get(device_id)
            if state is None:
                return 0.0
            if state["paused"]:
                return state["base_position_ms"]
            return state["base_position_ms"] + (
                float(master_ms) - state["base_master_ms"]
            )

    def render_position(self, device_id, master_ms):
        dev = self._device(device_id)
        return self.position(device_id, master_ms) - dev["latency_ms"]

    def pause(self, device_id, master_ms):
        with self._lock:
            self._device(device_id)
            state = self._playing.get(device_id)
            if state is None:
                state = {
                    "base_position_ms": 0.0,
                    "base_master_ms": float(master_ms),
                    "paused": False,
                }
                self._playing[device_id] = state
            if not state["paused"]:
                state["base_position_ms"] += (
                    float(master_ms) - state["base_master_ms"]
                )
                state["base_master_ms"] = float(master_ms)
                state["paused"] = True
            self._paused_at[device_id] = float(master_ms)

    def resume(self, device_id, master_ms):
        with self._lock:
            self._device(device_id)
            state = self._playing.get(device_id)
            if state is None:
                self.start(device_id, master_ms)
                return
            state["base_master_ms"] = float(master_ms)
            state["paused"] = False
            self._paused_at.pop(device_id, None)

    def join(self, device_id):
        with self._lock:
            dev = self._device(device_id)
            dev["buffer_ms"] = max(dev["buffer_ms"], self.target_buffer_ms)
            return {"state": "buffering", "buffer_ms": dev["buffer_ms"]}

    def resample_ratio(self, device_id):
        dev = self._device(device_id)
        return self.REFERENCE_RATE / float(dev["sample_rate"])

    def catchup_rate(self, device_id, buffer_ms):
        self._device(device_id)
        excess = float(buffer_ms) - self.target_buffer_ms
        if excess <= 0:
            return 1.0
        return min(self.MAX_CATCHUP_RATE, 1.0 + excess / 10000.0)

    def migrate_master(self, new_master, master_ms):
        with self._lock:
            if new_master not in self.devices:
                raise KeyError("unknown device: %r" % (new_master,))
            for device_id, state in self._playing.items():
                if not state["paused"]:
                    state["base_position_ms"] += (
                        float(master_ms) - state["base_master_ms"]
                    )
                state["base_master_ms"] = float(master_ms)
            self.master = new_master

    def correct_clock(self, device_id, measured_offset_ms):
        with self._lock:
            dev = self._device(device_id)
            step = max(
                -self.MAX_CORRECTION_STEP_MS,
                min(self.MAX_CORRECTION_STEP_MS, float(measured_offset_ms)),
            )
            pending = float(measured_offset_ms) - step
            dev["offset_ms"] -= step
            dev["pending_correction_ms"] = pending
            return {"applied_ms": step, "pending_ms": pending}

    def sync_round(self, reports, budget_ms=5.0):
        start = time.perf_counter()
        processed = 0
        with self._lock:
            for report in reports:
                dev = self.devices.get(report.get("device"))
                if dev is None:
                    continue
                offset = float(report.get("offset_ms", 0.0))
                dev["offset_ms"] = 0.5 * dev["offset_ms"] + 0.5 * offset
                processed += 1
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        estimated_ms = processed * self.MIN_COST_PER_REPORT_MS
        downshifted = False
        if elapsed_ms > budget_ms or estimated_ms > budget_ms:
            downshifted = True
            self.interval_ms = min(self.MAX_INTERVAL_MS, self.interval_ms * 2.0)
            self.over_budget_events.append(
                {
                    "elapsed_ms": elapsed_ms,
                    "budget_ms": budget_ms,
                    "interval_ms": self.interval_ms,
                }
            )
        return {
            "processed": processed,
            "elapsed_ms": elapsed_ms,
            "downshifted": downshifted,
            "interval_ms": self.interval_ms,
        }

    def snapshot(self):
        with self._lock:
            return {
                "master": self.master,
                "target_buffer_ms": self.target_buffer_ms,
                "interval_ms": self.interval_ms,
                "devices": copy.deepcopy(self.devices),
            }
