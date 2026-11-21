"""Render-target memory aliasing planner.

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

import copy
import threading
import time


def _align_up(value, alignment):
    if alignment <= 1:
        return value
    return ((value + alignment - 1) // alignment) * alignment


def _as_resource_tuple(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


class RtAliasPlan:
    def __init__(self, width=1920, height=1080, max_size=1 << 30,
                 tile_size=64, budget_ms=2.0):
        if width <= 0 or height <= 0:
            raise ValueError("resolution must be positive")
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        self.width = width
        self.height = height
        self.max_size = max_size
        self.tile_size = tile_size
        self.budget_ms = budget_ms

        self._resources = {}
        self._passes = []
        self._revision = 0
        self._cache_key = None
        self._cache_result = None
        self._lock = threading.RLock()

    def _invalidate(self):
        self._revision += 1
        self._cache_key = None
        self._cache_result = None

    def add_resource(self, rid, size, fmt="rgba8", samples=1, tile="linear",
                     external=False):
        with self._lock:
            if not isinstance(size, int) or isinstance(size, bool):
                raise ValueError("resource size must be an integer")
            if size <= 0:
                raise ValueError("resource size must be positive")
            if size > self.max_size:
                raise ValueError("resource size exceeds max_size")
            if not isinstance(samples, int) or isinstance(samples, bool):
                raise ValueError("sample count must be an integer")
            if samples <= 0:
                raise ValueError("sample count must be positive")
            if rid in self._resources:
                raise ValueError("duplicate resource id: %r" % (rid,))
            self._resources[rid] = {
                "id": rid,
                "size": size,
                "fmt": fmt,
                "samples": samples,
                "tile": tile,
                "external": bool(external),
                "order": len(self._resources),
            }
            self._invalidate()

    def add_pass(self, name, reads=(), writes=(), start=0, end=0):
        with self._lock:
            if not isinstance(start, int) or isinstance(start, bool):
                raise ValueError("pass start must be an integer")
            if not isinstance(end, int) or isinstance(end, bool):
                raise ValueError("pass end must be an integer")
            if start < 0 or end < start:
                raise ValueError("invalid pass interval")
            self._passes.append({
                "name": name,
                "reads": _as_resource_tuple(reads),
                "writes": _as_resource_tuple(writes),
                "start": start,
                "end": end,
                "index": len(self._passes),
            })
            self._invalidate()

    def set_resolution(self, width, height):
        with self._lock:
            if width <= 0 or height <= 0:
                raise ValueError("resolution must be positive")
            self.width = width
            self.height = height
            self._invalidate()

    def plan(self, inflight=None, elapsed_ms=0.0):
        if inflight is None:
            inflight = frozenset()
        elif isinstance(inflight, str):
            inflight = frozenset((inflight,))
        else:
            inflight = frozenset(inflight)
        with self._lock:
            started = time.perf_counter()
            cache_key = (self._revision, inflight)
            if self._cache_key == cache_key:
                result = copy.deepcopy(self._cache_result)
            else:
                result = self._build_plan(inflight)
                self._cache_key = cache_key
                self._cache_result = copy.deepcopy(result)

            measured_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms and elapsed_ms > 0:
                reported_ms = float(elapsed_ms)
            else:
                reported_ms = measured_ms
            result["elapsed_ms"] = reported_ms
            result["degraded"] = reported_ms > self.budget_ms
            return result

    @staticmethod
    def _lifetime_touches(passes, rid):
        touched = [
            p for p in passes
            if rid in p["reads"] or rid in p["writes"]
        ]
        if not touched:
            return {
                "start": 0,
                "end": -1,
                "first_index": len(passes),
                "last_index": -1,
            }
        return {
            "start": min(p["start"] for p in touched),
            "end": max(p["end"] for p in touched),
            "first_index": min(p["index"] for p in touched),
            "last_index": max(p["index"] for p in touched),
        }

    def _can_share(self, left, right):
        if left["external"] or right["external"]:
            return False
        if left["protected"] or right["protected"]:
            return False
        if (left["fmt"] != right["fmt"]
                or left["samples"] != right["samples"]
                or left["tile"] != right["tile"]):
            return False
        return (left["lifetime"]["end"] < right["lifetime"]["start"]
                or right["lifetime"]["end"] < left["lifetime"]["start"])

    def _build_plan(self, inflight):
        resources = []
        for rid, resource in self._resources.items():
            item = dict(resource)
            item.update(self._lifetime_touches(self._passes, rid))
            item["lifetime"] = {
                "start": item["start"],
                "end": item["end"],
            }
            item["protected"] = rid in inflight
            resources.append(item)

        pooled = [r for r in resources if not r["external"]]
        pooled.sort(key=lambda r: (
            r["lifetime"]["start"],
            r["lifetime"]["end"],
            -r["size"],
            r["order"],
        ))

        bins = []
        reuse_count = 0
        for resource in pooled:
            candidates = []
            for index, slot in enumerate(bins):
                if all(self._can_share(resource, member)
                       for member in slot["members"]):
                    candidates.append(index)

            if candidates:
                def score(index):
                    capacity = bins[index]["capacity"]
                    if capacity >= resource["size"]:
                        return (0, capacity - resource["size"])
                    return (1, resource["size"] - capacity)

                chosen = min(candidates, key=score)
                slot = bins[chosen]
                slot["members"].append(resource)
                slot["capacity"] = max(slot["capacity"], resource["size"])
                resource["bin"] = chosen
                reuse_count += 1
            else:
                slot = {
                    "members": [resource],
                    "capacity": resource["size"],
                    "offset": 0,
                }
                bins.append(slot)
                resource["bin"] = len(bins) - 1

        cursor = 0
        for slot in bins:
            slot["offset"] = _align_up(cursor, self.tile_size)
            cursor = slot["offset"] + _align_up(
                slot["capacity"], self.tile_size)

        allocation = {}
        for resource in resources:
            if resource["external"]:
                allocation[resource["id"]] = {
                    "offset": None,
                    "size": resource["size"],
                    "aliases": [],
                    "external": True,
                }
            else:
                slot = bins[resource["bin"]]
                allocation[resource["id"]] = {
                    "offset": slot["offset"],
                    "size": resource["size"],
                    "aliases": [],
                    "external": False,
                }

        for resource in resources:
            entry = allocation[resource["id"]]
            if entry["offset"] is None:
                continue
            entry["aliases"] = [
                other["id"] for other in resources
                if other["id"] != resource["id"]
                and not other["external"]
                and allocation[other["id"]]["offset"] == entry["offset"]
            ]

        barriers = self._build_barriers(resources, bins)
        return {
            "allocation": allocation,
            "barriers": barriers,
            "peak_bytes": cursor,
            "reuse_count": reuse_count,
            "revision": self._revision,
            "resolution": [self.width, self.height],
            "degraded": False,
            "elapsed_ms": 0.0,
        }

    def _build_barriers(self, resources, bins):
        barriers = []
        seen = set()

        def emit(from_index, to_index, resource_id):
            if from_index < 0 or to_index < 0:
                return
            if from_index >= to_index:
                return
            key = (from_index, to_index, resource_id)
            if key in seen:
                return
            seen.add(key)
            barriers.append({
                "from": from_index,
                "to": to_index,
                "resource": resource_id,
            })

        for resource in resources:
            rid = resource["id"]
            write_indices = sorted(
                p["index"] for p in self._passes if rid in p["writes"])
            read_indices = sorted(
                p["index"] for p in self._passes if rid in p["reads"])
            for read_index in read_indices:
                producers = [index for index in write_indices
                             if index < read_index]
                if producers:
                    emit(producers[-1], read_index, rid)

        for slot in bins:
            members = sorted(slot["members"], key=lambda r: (
                r["lifetime"]["start"],
                r["lifetime"]["end"],
                r["order"],
            ))
            for previous, current in zip(members, members[1:]):
                if previous["last_index"] < 0 or current["first_index"] >= len(self._passes):
                    continue
                emit(previous["last_index"], current["first_index"],
                     current["id"])

        barriers.sort(key=lambda item: (
            item["to"], item["from"], str(item["resource"])))
        return barriers
