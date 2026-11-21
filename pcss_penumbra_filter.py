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


class PcssPenumbraFilter:
    def __init__(self, resolution=2048, light_size=1.0, bias=0.01, max_samples=64, tile_capacity=64):
        self.resolution = resolution
        self.light_size = light_size
        self.bias = bias
        self.max_samples = max_samples
        self.tile_capacity = tile_capacity

    def set_resolution(self, size):
        raise NotImplementedError

    def penumbra_size(self, blocker_depth, receiver_depth, light_size=None):
        raise NotImplementedError

    def guard_texels(self, penumbra_world):
        raise NotImplementedError

    def estimate(self, receiver_depth, blockers, light_size=None, bias=None):
        raise NotImplementedError

    def jitter(self, index, count):
        raise NotImplementedError

    def tile_for(self, light_id):
        raise NotImplementedError
