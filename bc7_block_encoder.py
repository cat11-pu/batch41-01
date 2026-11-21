"""BC7-style 4x4 block encoder.

Image input: ``{"id", "width", "height", "pixels"}`` where ``pixels`` is a
row-major list of (r, g, b, a) tuples in 0..255.

Contract used by the tests
--------------------------
* ``encode_image(image, mip_levels=1)`` returns
  ``{"id", "width", "height", "blocks", "mode_counts", "mips", "stats"}``.
  ``encode`` and ``decode`` are aliases of ``encode_image`` and
  ``decode_image``.
  Blocks are 4x4.  A block whose pixels all have alpha 255 uses mode
  "opaque"; any pixel with alpha < 255 makes it "alpha".
* Endpoints are the per-channel min and max of the block's sRGB values and
  each pixel is rebuilt from the nearest of the four levels
  (t in {0, 1/3, 2/3, 1}).  Uniform blocks (min == max) reconstruct exactly
  and never divide by zero.
* ``encode_page`` returns ``{"id", "width", "height", "blocks",
  "mode_counts", "edge_padding"}`` with no timing; it is deterministic under
  concurrent calls.
* ``decode_image`` / ``decode_page`` return the reconstructed image cropped
  to the source size.
* ``build_mips`` / ``mips`` are generated from the source image before block
  compression: level 0 is the source, each next level is a 2x2 box average
  of the previous level (rounded).  ``stats["mip_source"] == "source"``.
* Edge blocks replicate the last row/column (``edge_padding == "replicate"``).
* With BC7 support off (``set_supported(False)``) blocks use ``fallback`` as
  their ``format`` and stay decodable; if ``fallback`` is None encoding
  raises ``ValueError``.
* ``reconstruct_normal(x, y)`` returns a unit (x, y, z) with z >= 0 and
  renormalizes inputs outside the unit circle.
* ``stats`` of one image: ``{"mean_error", "max_error", "chroma_error",
  "blocks", "fallback_blocks", "mip_source", "elapsed_ms"}``; the cumulative
  ``encoder.stats()`` returns ``{"pages", "blocks", "fallback_blocks"}``.
"""

import math
import threading
import time


LEVEL_T = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)
EDGE_PADDING = "replicate"


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _clamp(value, high):
    if value < 0:
        return 0
    if value >= high:
        return high - 1
    return value


class Bc7BlockEncoder:
    def __init__(self, fallback="bc3", srgb=True):
        self.fallback = fallback
        self.srgb = srgb
        self._supported = True
        self._stats_lock = threading.Lock()
        self._locks_guard = threading.Lock()
        self._block_locks = {}
        self._pages = 0
        self._blocks = 0
        self._fallback_blocks = 0

    def set_supported(self, flag):
        self._supported = bool(flag)

    def encode(self, image, mip_levels=1):
        return self.encode_image(image, mip_levels=mip_levels)

    def decode(self, encoded):
        return self.decode_image(encoded)

    def encode_page(self, image):
        if not self._supported and self.fallback is None:
            raise ValueError("BC7 is unsupported and no fallback format is configured")
        page = self._encode_page(image)
        return {
            "id": image["id"],
            "width": image["width"],
            "height": image["height"],
            "blocks": page["blocks"],
            "mode_counts": page["mode_counts"],
            "edge_padding": EDGE_PADDING,
        }

    def decode_page(self, encoded):
        return self._decode(encoded)

    def encode_image(self, image, mip_levels=1):
        started = time.perf_counter()
        if not self._supported and self.fallback is None:
            raise ValueError("BC7 is unsupported and no fallback format is configured")

        mips = self.build_mips(image, mip_levels)
        page = self._encode_page(image)
        decoded = self._decode(page)

        stats = self._error_stats(image, decoded)
        stats["blocks"] = len(page["blocks"])
        stats["fallback_blocks"] = sum(1 for block in page["blocks"] if block["format"] != "bc7")
        stats["mip_source"] = "source"
        stats["elapsed_ms"] = (time.perf_counter() - started) * 1000.0

        return {
            "id": image["id"],
            "width": image["width"],
            "height": image["height"],
            "blocks": page["blocks"],
            "mode_counts": page["mode_counts"],
            "edge_padding": EDGE_PADDING,
            "mips": mips,
            "stats": stats,
        }

    def decode_image(self, encoded):
        return self._decode(encoded)

    def build_mips(self, image, levels):
        if levels < 1:
            raise ValueError("mip level count must be at least 1")
        mips = [
            {
                "width": image["width"],
                "height": image["height"],
                "pixels": [row[:] for row in image["pixels"]],
            }
        ]
        for _ in range(1, levels):
            mips.append(self._downsample(mips[-1]))
        return mips

    def mips(self, image, levels):
        return self.build_mips(image, levels)

    def reconstruct_normal(self, x, y):
        z_squared = 1.0 - x * x - y * y
        if z_squared < 0.0:
            length = math.hypot(x, y)
            if length == 0.0:
                return (0.0, 0.0, 1.0)
            x = x / length
            y = y / length
            z = 0.0
        else:
            z = math.sqrt(z_squared)
        return (x, y, z)

    def stats(self):
        with self._stats_lock:
            return {
                "pages": self._pages,
                "blocks": self._blocks,
                "fallback_blocks": self._fallback_blocks,
            }

    def _downsample(self, level):
        width = level["width"] // 2
        height = level["height"] // 2
        pixels = []
        for out_y in range(height):
            row = []
            src_y = out_y * 2
            for out_x in range(width):
                src_x = out_x * 2
                averaged = []
                for channel in range(4):
                    total = (
                        level["pixels"][src_y][src_x][channel]
                        + level["pixels"][src_y][src_x + 1][channel]
                        + level["pixels"][src_y + 1][src_x][channel]
                        + level["pixels"][src_y + 1][src_x + 1][channel]
                    )
                    averaged.append(_round_half_up(total / 4.0))
                row.append(tuple(averaged))
            pixels.append(row)
        return {"width": width, "height": height, "pixels": pixels}

    def _sample_pixel(self, image, x, y):
        x = _clamp(x, image["width"])
        y = _clamp(y, image["height"])
        return image["pixels"][y][x]

    def _block_lock(self, image_id, block_x, block_y):
        key = (image_id, block_x, block_y)
        with self._locks_guard:
            lock = self._block_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._block_locks[key] = lock
        return lock

    def _encode_page(self, image):
        columns = (image["width"] + 3) // 4
        rows = (image["height"] + 3) // 4
        block_format = "bc7" if self._supported else self.fallback
        blocks = []
        mode_counts = {"opaque": 0, "alpha": 0}
        for block_y in range(rows):
            for block_x in range(columns):
                lock = self._block_lock(image["id"], block_x, block_y)
                with lock:
                    block = self._encode_block(image, block_x, block_y, block_format)
                blocks.append(block)
                mode_counts[block["mode"]] += 1

        fallback_blocks = sum(1 for block in blocks if block["format"] != "bc7")
        with self._stats_lock:
            self._pages += 1
            self._blocks += len(blocks)
            self._fallback_blocks += fallback_blocks
        return {
            "id": image["id"],
            "width": image["width"],
            "height": image["height"],
            "blocks": blocks,
            "mode_counts": mode_counts,
        }

    def _encode_block(self, image, block_x, block_y, block_format):
        origin_x = block_x * 4
        origin_y = block_y * 4
        grid = [
            self._sample_pixel(image, origin_x + local_x, origin_y + local_y)
            for local_y in range(4)
            for local_x in range(4)
        ]
        mode = "alpha" if any(pixel[3] < 255 for pixel in grid) else "opaque"
        low = tuple(min(pixel[channel] for pixel in grid) for channel in range(4))
        high = tuple(max(pixel[channel] for pixel in grid) for channel in range(4))

        indices = []
        for pixel in grid:
            best_index = 0
            best_distance = None
            for index, t in enumerate(LEVEL_T):
                rebuilt = self._interpolate(low, high, t)
                distance = sum((pixel[channel] - rebuilt[channel]) ** 2 for channel in range(4))
                if best_distance is None or distance < best_distance:
                    best_distance = distance
                    best_index = index
            indices.append(best_index)

        return {
            "bx": block_x,
            "by": block_y,
            "mode": mode,
            "format": block_format,
            "endpoints": {"lo": low, "hi": high},
            "indices": indices,
        }

    @staticmethod
    def _interpolate(low, high, t):
        return tuple(
            low[channel] + _round_half_up(t * (high[channel] - low[channel]))
            for channel in range(4)
        )

    def _decode(self, encoded):
        columns = (encoded["width"] + 3) // 4
        rows = (encoded["height"] + 3) // 4
        grid = [[None] * (columns * 4) for _ in range(rows * 4)]
        for block in encoded["blocks"]:
            low = block["endpoints"]["lo"]
            high = block["endpoints"]["hi"]
            for slot, index in enumerate(block["indices"]):
                local_x = slot % 4
                local_y = slot // 4
                grid[block["by"] * 4 + local_y][block["bx"] * 4 + local_x] = (
                    self._interpolate(low, high, LEVEL_T[index])
                )

        pixels = [
            [grid[y][x] for x in range(encoded["width"])]
            for y in range(encoded["height"])
        ]
        return {
            "id": encoded["id"],
            "width": encoded["width"],
            "height": encoded["height"],
            "pixels": pixels,
        }

    def _error_stats(self, source, decoded):
        total_error = 0.0
        total_chroma = 0.0
        max_error = 0.0
        pixel_count = 0
        for y in range(source["height"]):
            for x in range(source["width"]):
                original = source["pixels"][y][x]
                rebuilt = decoded["pixels"][y][x]
                error = max(abs(original[channel] - rebuilt[channel]) for channel in range(4))
                max_error = max(max_error, float(error))
                total_error += error
                original_chroma = max(original[:3]) - min(original[:3])
                rebuilt_chroma = max(rebuilt[:3]) - min(rebuilt[:3])
                total_chroma += abs(original_chroma - rebuilt_chroma)
                pixel_count += 1
        return {
            "mean_error": total_error / pixel_count,
            "max_error": max_error,
            "chroma_error": total_chroma / pixel_count,
        }
