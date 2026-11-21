"""PCSS penumbra filter.

Contract used by the tests
--------------------------
Depths are world distances from the light along the light ray.
* ``penumbra_size(blocker_depth, receiver_depth, light_size=None)``:
  ``light_size * (receiver_depth - blocker_depth) / blocker_depth`` in world
  units; ``light_size == 0`` returns 0.0 (hard shadow, no division by zero).
  The world size does not depend on the shadow map resolution.
* ``set_resolution(size)`` changes the shadow map resolution; the world
  extent is fixed at 256 world units, so ``texel_world = 256 / size``.
* ``guard_texels(penumbra_world)`` is ``ceil(penumbra_world / texel_world)``,
  at least 1.
* ``estimate(receiver_depth, blockers, light_size=None, bias=None)``:
  blockers are ``{"depth", "opacity"}``.  A blocker is counted when
  ``0 < depth <= receiver_depth`` and ``receiver_depth - depth > bias``.
  ``occlusion`` is the mean opacity of counted blockers (0 when none),
  ``visibility = 1 - occlusion`` clamped to [0, 1]; ``samples`` is the number
  of counted blockers, capped at ``max_samples`` (only the first ones count).
  ``penumbra`` is the mean penumbra size over counted blockers, 0 with none.
* ``jitter(index, count)`` returns a deterministic list of ``count`` floats in
  [0, 1), identical for identical inputs.
* ``tile_for(light_id)`` assigns a permanent unique atlas tile; running out of
  ``tile_capacity`` tiles raises ``ValueError``.
"""

import math
import threading


class PcssPenumbraFilter:
    WORLD_EXTENT = 256.0

    def __init__(self, resolution=2048, light_size=1.0, bias=0.01, max_samples=64, tile_capacity=64):
        self._tile_lock = threading.Lock()
        self._tiles = {}
        self.set_resolution(resolution)
        self.light_size = light_size
        self.bias = bias
        self.max_samples = max_samples
        self.tile_capacity = tile_capacity

    def set_resolution(self, size):
        if size <= 0:
            raise ValueError("shadow map resolution must be positive")
        self.resolution = size
        self.texel_world = self.WORLD_EXTENT / size

    def penumbra_size(self, blocker_depth, receiver_depth, light_size=None):
        if light_size is None:
            light_size = self.light_size
        if light_size == 0:
            return 0.0
        if blocker_depth <= 0 or receiver_depth <= blocker_depth:
            return 0.0
        return light_size * (receiver_depth - blocker_depth) / blocker_depth

    def guard_texels(self, penumbra_world):
        if penumbra_world <= 0:
            return 1
        return max(1, int(math.ceil(penumbra_world / self.texel_world)))

    def estimate(self, receiver_depth, blockers, light_size=None, bias=None):
        if light_size is None:
            light_size = self.light_size
        if bias is None:
            bias = self.bias

        sample_limit = max(0, self.max_samples)
        samples = 0
        opacity_sum = 0.0
        penumbra_sum = 0.0

        for blocker in blockers:
            if samples >= sample_limit:
                break

            depth = blocker["depth"]
            if not (0 < depth <= receiver_depth and receiver_depth - depth > bias):
                continue

            opacity = float(blocker.get("opacity", 1.0))
            opacity_sum += opacity
            penumbra_sum += self.penumbra_size(depth, receiver_depth, light_size)
            samples += 1

        if samples == 0:
            return {"visibility": 1.0, "penumbra": 0.0, "samples": 0}

        occlusion = opacity_sum / samples
        visibility = min(1.0, max(0.0, 1.0 - occlusion))
        return {
            "visibility": visibility,
            "penumbra": penumbra_sum / samples,
            "samples": samples,
        }

    def jitter(self, index, count):
        if count <= 0:
            return []

        state = int(index) & 0xFFFFFFFFFFFFFFFF
        values = []
        for _ in range(count):
            state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
            mixed = state
            mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
            mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
            mixed ^= mixed >> 31
            values.append((mixed >> 11) * (1.0 / (1 << 53)))
        return values

    def tile_for(self, light_id):
        with self._tile_lock:
            tile = self._tiles.get(light_id)
            if tile is not None:
                return tile
            if len(self._tiles) >= self.tile_capacity:
                raise ValueError("shadow atlas tile capacity exhausted")
            tile = len(self._tiles)
            self._tiles[light_id] = tile
            return tile
