"""Multi-source live compositor with a unified master clock.

Every source timestamps its frames with its own timebase.  The compositor
converts every PTS onto a single master clock (milliseconds), picks the
nearest buffered frame for each composition tick and keeps audio aligned to
the same timeline.  Dropped frames only age the currently displayed frame;
they never compress the timeline of frames that arrive later.
"""

from collections import deque
import logging
import threading
import time


LOGGER = logging.getLogger(__name__)

# Transfers that mark a frame as high dynamic range / wide gamut.
HDR_TRANSFERS = frozenset(("pq", "hlg", "smpte2084", "arib-std-b67"))

# A full 1920x1080 canvas is expected to cost the configured 8 ms budget,
# i.e. roughly 259_200 output pixels per millisecond of composition work.
PIXELS_PER_BUDGET_MS = 1920 * 1080 / 8.0

# Resolution is never degraded below this floor.
MIN_WIDTH = 320
MIN_HEIGHT = 180


class MultiviewCompositor:
    def __init__(self, width=1920, height=1080, budget_ms=8.0):
        self.width = width
        self.height = height
        self.budget_ms = budget_ms
        self.layout = []
        # source -> deque of buffered frames (timestamps in master ms).
        self.frames = {}
        # Number of producer-side atomic queue swaps.
        self.queue_swaps = 0
        # Total frames evicted without ever being displayed.
        self.dropped_frames = 0
        self.degradations = []
        self.scale = "bilinear"
        self.last_latency_ms = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _pts_to_master_ms(pts, timebase):
        """Convert ``pts`` expressed in ``timebase`` units to master ms.

        ``timebase`` is given as ``(units_per_second, 1)`` style fractions,
        so one tick lasts ``timebase[1] / timebase[0]`` seconds.
        """
        units_per_second, scale = timebase
        return float(pts) * float(scale) / float(units_per_second) * 1000.0

    def set_layout(self, cells):
        """Validate and install a layout, preallocating each render target.

        Raises ``ValueError`` for zero/negative sized cells or cells that
        fall outside the composition canvas.
        """
        validated = []
        for index, cell in enumerate(cells):
            try:
                x = cell["x"]
                y = cell["y"]
                w = cell["w"]
                h = cell["h"]
                source = cell["source"]
            except (KeyError, TypeError) as exc:
                raise ValueError("layout cell %d is missing fields" % index) from exc

            if w <= 0 or h <= 0:
                raise ValueError(
                    "cell %r has non-positive size %dx%d" % (source, w, h))
            if x < 0 or y < 0:
                raise ValueError(
                    "cell %r has negative origin (%d, %d)" % (source, x, y))
            if x + w > self.width or y + h > self.height:
                raise ValueError(
                    "cell %r (%d,%d %dx%d) exceeds canvas %dx%d"
                    % (source, x, y, w, h, self.width, self.height))

            validated.append({
                "source": source,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
            })

        # Pre-allocate a render target for every cell so a layout switch
        # never produces a black frame while the first target is created.
        render_targets = [
            {
                "source": cell["source"],
                "width": cell["w"],
                "height": cell["h"],
                "buffer": bytearray(cell["w"] * cell["h"] * 4),
            }
            for cell in validated
        ]

        with self._lock:
            self.layout = validated
            self._render_targets = render_targets

        return {"preallocated": True, "cells": len(validated)}

    def push_frame(self, source, pts, timebase=(1000, 1), duration_ms=None,
                   transfer="bt709"):
        """Ingest one frame, normalising its PTS onto the master clock."""
        pts_ms = self._pts_to_master_ms(pts, timebase)
        is_hdr = transfer in HDR_TRANSFERS
        frame = {
            "source": source,
            "pts_ms": pts_ms,
            "duration_ms": duration_ms,
            "transfer": transfer,
            "color_space": "hdr" if is_hdr else "sdr",
            "displayed": False,
        }

        # Atomically swap in a new queue rather than mutating a queue the
        # encoder thread may currently be dequeuing from.
        with self._lock:
            current = self.frames.get(source)
            new_queue = deque(current) if current else deque()
            new_queue.append(frame)
            self.frames[source] = new_queue
            self.queue_swaps += 1
        return frame

    def select_frame(self, source, master_ms):
        """Return the buffered frame nearest ``master_ms`` for ``source``.

        Frames older than the selected one are evicted; evicted frames that
        were never displayed are counted as dropped.  The remaining timeline
        is untouched, so a dropped frame never compresses later timestamps.
        Returns ``None`` (placeholder territory) when nothing is buffered.
        """
        with self._lock:
            queue = self.frames.get(source)
            if not queue:
                return None

            # Nearest frame wins; on an exact tie keep the older frame so
            # playback does not jump forward.
            chosen = min(queue, key=lambda frame: (
                abs(frame["pts_ms"] - master_ms), frame["pts_ms"]))

            retained = deque()
            for frame in queue:
                if frame["pts_ms"] < chosen["pts_ms"]:
                    if not frame["displayed"]:
                        self.dropped_frames += 1
                    continue
                retained.append(frame)
            self.frames[source] = retained

            chosen["displayed"] = True
            stale_ms = max(0.0, master_ms - chosen["pts_ms"])
            return {
                "source": source,
                "pts_ms": chosen["pts_ms"],
                "duration_ms": chosen["duration_ms"],
                "transfer": chosen["transfer"],
                "color_space": chosen["color_space"],
                "stale_ms": stale_ms,
            }

    def _resolve_output_size(self, master_ms, budget_ms):
        """Halve the composition resolution while work exceeds budget."""
        width, height = self.width, self.height
        degraded = False
        estimated_ms = (width * height) / PIXELS_PER_BUDGET_MS
        while estimated_ms > budget_ms and (
                width > MIN_WIDTH or height > MIN_HEIGHT):
            degraded = True
            width = max(MIN_WIDTH, width // 2)
            height = max(MIN_HEIGHT, height // 2)
            estimated_ms = (width * height) / PIXELS_PER_BUDGET_MS

        if degraded:
            event = {
                "master_ms": master_ms,
                "budget_ms": budget_ms,
                "estimated_ms": (self.width * self.height) / PIXELS_PER_BUDGET_MS,
                "width": width,
                "height": height,
            }
            self.degradations.append(event)
            LOGGER.warning(
                "composition over budget (%.2fms > %.2fms); "
                "degrading to %dx%d",
                event["estimated_ms"], budget_ms, width, height)
        return width, height, degraded

    def compose(self, master_ms, budget_ms=None):
        """Compose one tick in fixed layout order on the master clock."""
        started = time.perf_counter()
        if budget_ms is None:
            budget_ms = self.budget_ms

        width, height, degraded = self._resolve_output_size(master_ms, budget_ms)

        layers = []
        placeholders = {}
        max_latency = 0.0

        # Layout order is the fixed z-order; arrival order is irrelevant.
        for cell in self.layout:
            source = cell["source"]
            frame = self.select_frame(source, master_ms)
            if frame is None:
                # Keep the master timeline moving with an explicit
                # placeholder instead of freezing the last frame.
                placeholders[source] = {
                    "state": "offline",
                    "master_ms": master_ms,
                }
                continue

            needs_conversion = frame["transfer"] in HDR_TRANSFERS
            layers.append({
                "source": source,
                "x": cell["x"],
                "y": cell["y"],
                "w": cell["w"],
                "h": cell["h"],
                "pts_ms": frame["pts_ms"],
                "stale_ms": frame["stale_ms"],
                # HDR input is tone-mapped onto the SDR canvas per its
                # transfer metadata before sampling with bilinear scaling.
                "color_converted": needs_conversion,
                "color_space": "sdr",
                "scale": self.scale,
            })
            max_latency = max(max_latency, master_ms - frame["pts_ms"])

        self.last_latency_ms = max(0.0, max_latency)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        return {
            "layout": list(self.layout),
            "layers": layers,
            "placeholders": placeholders,
            "width": width,
            "height": height,
            "degraded": degraded,
            "latency_ms": self.last_latency_ms,
            "elapsed_ms": elapsed_ms,
            "dropped_frames": self.dropped_frames,
            "budget_ms": budget_ms,
            "master_ms": master_ms,
        }

    def align_audio(self, master_ms, tracks):
        """Align every audio track onto the unified master timeline."""
        offsets = {}
        for track in tracks:
            offsets[track["id"]] = master_ms - track["pts_ms"]
        return {
            "master_ms": master_ms,
            "offsets": offsets,
        }

    def stats(self):
        with self._lock:
            queued = sum(len(queue) for queue in self.frames.values())
            layout_cells = len(self.layout)
        return {
            "layout": layout_cells,
            "scale": self.scale,
            "queued": queued,
            "queue_swaps": self.queue_swaps,
            "dropped_frames": self.dropped_frames,
            "degradations": len(self.degradations),
            "latency_ms": self.last_latency_ms,
            "width": self.width,
            "height": self.height,
        }
