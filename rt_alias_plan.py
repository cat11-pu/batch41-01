"""Render-target memory aliasing planner (initial stub).

Public API::

    RtAliasPlan(width=1920, height=1080, max_size=1 << 30, tile_size=64)
        .add_resource(rid, size, fmt="rgba8", samples=1, tile="linear",
                      external=False)
        .add_pass(name, reads=(), writes=(), start=0, end=0)
        .set_resolution(width, height)
        .plan(inflight=None, elapsed_ms=0.0) -> dict

`plan` returns::

    {
        "allocation": {rid: {"offset": int | None, "size": int,
                             "aliases": [rid, ...], "external": bool}},
        "barriers": [{"from": pass_index, "to": pass_index,
                      "resource": rid}, ...],
        "peak_bytes": int,
        "reuse_count": int,
        "revision": int,
        "resolution": [width, height],
        "degraded": bool,
        "elapsed_ms": float,
    }

Behaviour enforced by the checks:

* lifetimes are [first pass start, last pass end]; two resources may share
  memory only when `end_a < start_b` (or the mirrored order), so resources
  living through the same pass never alias;
* format, sample count and tile mode must match before aliasing;
* a resource that is written and read inside one pass keeps the whole pass in
  its lifetime and cannot alias with another resource of that pass;
* barriers are emitted between a producer pass and a later consumer pass;
* resources listed in `inflight` are not reused this frame;
* external resources stay outside the pool (`offset` None) and out of the peak;
* `reuse_count` counts resources that reuse an existing slot, and each
  allocation's `aliases` lists every other resource sharing its offset;
* every offset is aligned to `tile_size`;
* zero-size resources and resources over `max_size` raise ValueError;
* `set_resolution` bumps the revision so the next plan is recomputed;
* planning is serialised, so concurrent calls agree.
"""


class RtAliasPlan:
    def __init__(self, width=1920, height=1080, max_size=1 << 30,
                 tile_size=64):
        self.width = width
        self.height = height
        self.max_size = max_size
        self.tile_size = tile_size

    def add_resource(self, rid, size, fmt="rgba8", samples=1, tile="linear",
                     external=False):
        raise NotImplementedError

    def add_pass(self, name, reads=(), writes=(), start=0, end=0):
        raise NotImplementedError

    def set_resolution(self, width, height):
        raise NotImplementedError

    def plan(self, inflight=None, elapsed_ms=0.0):
        raise NotImplementedError
