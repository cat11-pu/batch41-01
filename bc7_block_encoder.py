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


class Bc7BlockEncoder:
    def __init__(self, fallback="bc3", srgb=True):
        self.fallback = fallback
        self.srgb = srgb

    def set_supported(self, flag):
        raise NotImplementedError

    def encode(self, image, mip_levels=1):
        raise NotImplementedError

    def decode(self, encoded):
        raise NotImplementedError

    def encode_page(self, image):
        raise NotImplementedError

    def decode_page(self, encoded):
        raise NotImplementedError

    def encode_image(self, image, mip_levels=1):
        raise NotImplementedError

    def decode_image(self, encoded):
        raise NotImplementedError

    def build_mips(self, image, levels):
        raise NotImplementedError

    def reconstruct_normal(self, x, y):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
