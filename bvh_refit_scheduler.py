"""Bounded-budget BVH dirty/refit scheduling.

Public API::

    BvhRefitScheduler(budget_ms=2.0, leaf_size=4)
        .build(primitives) -> revision
        .mark_dirty(pid, priority=0)
        .update_primitive(pid, verts=None, translate=None)   # marks dirty
        .refit(elapsed_ms=0.0) -> dict
        .query(origin, direction, revision=None) -> [{"id", "t"}, ...]
        .remove(pid)
        .compact() -> dict
        .stats() -> dict
        .root_bounds(revision=None) -> ((minx, miny, minz), (maxx, maxy, maxz))

A primitive is::

    {"id": hashable, "verts": [[x, y, z], [x, y, z], [x, y, z]],
     "translate": [x, y, z]}

The scheduler keeps immutable tree revisions.  ``refit`` builds a new tree
from the selected dirty primitives and atomically publishes it, while readers
continue to traverse the previous revision.  Uncommitted dirty primitives keep
their previous published geometry and remain dirty for the next refit.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import threading
import time


_EPS = 1e-12
_HIT_EPS = 1e-9
_RELATIVE_AREA_EPS = 1e-24
_INF = float("inf")


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _as_vec3(value, label):
    if value is None or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three coordinates")
    result = []
    for coordinate in value:
        try:
            number = float(coordinate)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} contains a non-numeric coordinate") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} contains a non-finite coordinate")
        result.append(number)
    return tuple(result)


def _is_degenerate(verts):
    edge_a = _sub(verts[1], verts[0])
    edge_b = _sub(verts[2], verts[0])
    normal = _cross(edge_a, edge_b)
    area_squared = _dot(normal, normal)
    scale_squared = max(_dot(edge_a, edge_a), _dot(edge_b, edge_b))
    if scale_squared <= 0.0:
        return True
    denominator = scale_squared * scale_squared
    if denominator == 0.0:
        return True
    if math.isinf(denominator):
        return area_squared == 0.0
    return area_squared / denominator <= _RELATIVE_AREA_EPS


def _union_bounds(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return (
        (
            min(left[0][0], right[0][0]),
            min(left[0][1], right[0][1]),
            min(left[0][2], right[0][2]),
        ),
        (
            max(left[1][0], right[1][0]),
            max(left[1][1], right[1][1]),
            max(left[1][2], right[1][2]),
        ),
    )


def _primitive_bounds(verts, translate):
    tx, ty, tz = translate
    xs = [vertex[0] + tx for vertex in verts]
    ys = [vertex[1] + ty for vertex in verts]
    zs = [vertex[2] + tz for vertex in verts]
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def _empty_bounds():
    return ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))


@dataclass(frozen=True, slots=True)
class _Primitive:
    id: object
    verts: tuple
    translate: tuple
    bounds: tuple
    order: int


@dataclass(slots=True)
class _Record:
    id: object
    verts: tuple
    translate: tuple
    order: int


@dataclass(frozen=True, slots=True)
class _Node:
    id: int
    bounds: tuple
    left: object = None
    right: object = None
    primitives: tuple = ()


@dataclass(frozen=True, slots=True)
class _Snapshot:
    revision: int
    root: object
    states: dict
    leaf_paths: dict
    node_count: int

    def bounds(self):
        return self.root.bounds if self.root is not None else _empty_bounds()


class _TreeBuilder:
    def __init__(self, leaf_size):
        self.leaf_size = leaf_size
        self._next_id = 0
        self.node_count = 0
        self.leaf_paths = {}

    def build(self, primitives):
        if not primitives:
            return None
        return self._build(list(primitives), ())

    def _build(self, primitives, parent_path):
        bounds = None
        for primitive in primitives:
            bounds = _union_bounds(bounds, primitive.bounds)

        node_id = self._next_id
        self._next_id += 1
        self.node_count += 1
        path = parent_path + (node_id,)

        if len(primitives) <= self.leaf_size:
            for primitive in primitives:
                self.leaf_paths[primitive.id] = path
            return _Node(node_id, bounds, primitives=tuple(primitives))

        centroids = [
            (
                (primitive.bounds[0][0] + primitive.bounds[1][0]) * 0.5,
                (primitive.bounds[0][1] + primitive.bounds[1][1]) * 0.5,
                (primitive.bounds[0][2] + primitive.bounds[1][2]) * 0.5,
            )
            for primitive in primitives
        ]
        spreads = [
            max(point[axis] for point in centroids)
            - min(point[axis] for point in centroids)
            for axis in range(3)
        ]
        split_axis = max(range(3), key=lambda axis: spreads[axis])

        if spreads[split_axis] <= _EPS:
            primitives.sort(key=lambda primitive: primitive.order)
        else:
            primitives.sort(
                key=lambda primitive: (
                    (primitive.bounds[0][split_axis] + primitive.bounds[1][split_axis])
                    * 0.5,
                    primitive.order,
                )
            )

        middle = len(primitives) // 2
        left = self._build(primitives[:middle], path)
        right = self._build(primitives[middle:], path)
        return _Node(node_id, bounds, left=left, right=right)


class BvhRefitScheduler:
    def __init__(self, budget_ms=2.0, leaf_size=4):
        self.budget_ms = max(0.0, float(budget_ms))
        self.leaf_size = max(1, int(leaf_size))
        self._lock = threading.RLock()
        self._records = {}
        self._dirty = {}
        self._dirty_nodes = set()
        self._removed = set()
        self._degenerate = set()
        self._snapshots = OrderedDict()
        self._latest_revision = 0
        self._pruned_count = 0
        self._last_updated_nodes = 0
        self._history_limit = 2

    def _parse_primitive(self, primitive, order):
        if not isinstance(primitive, dict):
            raise ValueError("primitive must be a mapping")
        if "id" not in primitive:
            raise ValueError("primitive is missing an id")
        pid = primitive["id"]
        try:
            hash(pid)
        except TypeError as exc:
            raise ValueError("primitive id must be hashable") from exc

        raw_verts = primitive.get("verts")
        if raw_verts is None or len(raw_verts) != 3:
            raise ValueError("a primitive must have exactly three vertices")
        verts = tuple(_as_vec3(vertex, "vertex") for vertex in raw_verts)
        raw_translate = primitive.get("translate", (0.0, 0.0, 0.0))
        translate = _as_vec3(raw_translate, "translate")

        if _is_degenerate(verts):
            return None

        bounds = _primitive_bounds(verts, translate)
        if not all(
            math.isfinite(coordinate)
            for corner in bounds
            for coordinate in corner
        ):
            raise ValueError("primitive bounds are not finite")
        return _Primitive(pid, verts, translate, bounds, order)

    def _make_snapshot(self, revision, states):
        builder = _TreeBuilder(self.leaf_size)
        ordered = sorted(states.values(), key=lambda primitive: primitive.order)
        root = builder.build(ordered)
        return _Snapshot(
            revision=revision,
            root=root,
            states=dict(states),
            leaf_paths=builder.leaf_paths,
            node_count=builder.node_count,
        )

    def _store_snapshot(self, snapshot):
        self._snapshots[snapshot.revision] = snapshot
        self._latest_revision = snapshot.revision
        while len(self._snapshots) > self._history_limit:
            self._snapshots.popitem(last=False)

    def _mark_ancestors(self, pid):
        snapshot = self._snapshots.get(self._latest_revision)
        if snapshot is None:
            return
        self._dirty_nodes.update(snapshot.leaf_paths.get(pid, ()))

    def build(self, primitives):
        with self._lock:
            parsed = []
            seen = set()
            pruned = 0
            for order, primitive in enumerate(primitives):
                candidate = self._parse_primitive(primitive, order)
                if candidate is None:
                    pruned += 1
                    continue
                if candidate.id in seen:
                    raise ValueError(f"duplicate primitive id: {candidate.id!r}")
                seen.add(candidate.id)
                parsed.append(candidate)

            self._records = {
                primitive.id: _Record(
                    primitive.id,
                    primitive.verts,
                    primitive.translate,
                    primitive.order,
                )
                for primitive in parsed
            }
            self._dirty.clear()
            self._dirty_nodes.clear()
            self._removed.clear()
            self._degenerate.clear()
            self._snapshots.clear()
            self._pruned_count = pruned
            self._last_updated_nodes = 0

            revision = self._latest_revision + 1
            states = {primitive.id: primitive for primitive in parsed}
            snapshot = self._make_snapshot(revision, states)
            self._store_snapshot(snapshot)
            return revision

    def mark_dirty(self, pid, priority=0):
        with self._lock:
            if pid not in self._records:
                raise KeyError(pid)
            try:
                numeric_priority = float(priority)
            except (TypeError, ValueError) as exc:
                raise ValueError("priority must be numeric") from exc
            if not math.isfinite(numeric_priority):
                raise ValueError("priority must be finite")
            previous = self._dirty.get(pid)
            if previous is None or numeric_priority > previous:
                self._dirty[pid] = numeric_priority
            self._mark_ancestors(pid)

    def update_primitive(self, pid, verts=None, translate=None):
        with self._lock:
            record = self._records.get(pid)
            if record is None:
                raise KeyError(pid)

            new_verts = record.verts
            if verts is not None:
                if len(verts) != 3:
                    raise ValueError("a primitive must have exactly three vertices")
                new_verts = tuple(_as_vec3(vertex, "vertex") for vertex in verts)

            new_translate = record.translate
            if translate is not None:
                new_translate = _as_vec3(translate, "translate")

            self._records[pid] = _Record(
                record.id, new_verts, new_translate, record.order
            )
            if pid not in self._dirty:
                self._dirty[pid] = 0.0
            self._mark_ancestors(pid)

    def _select_dirty(self, elapsed_ms):
        dirty_ids = list(self._dirty)
        if not dirty_ids:
            return [], False

        over_budget = elapsed_ms > self.budget_ms
        if not over_budget:
            return dirty_ids, False

        cost_per_item = elapsed_ms / len(dirty_ids)
        allowance = max(1, int(self.budget_ms / cost_per_item))
        dirty_ids.sort(
            key=lambda pid: (-self._dirty[pid], self._records[pid].order)
        )
        return dirty_ids[:allowance], True

    def _make_primitive(self, record):
        return _Primitive(
            record.id,
            record.verts,
            record.translate,
            _primitive_bounds(record.verts, record.translate),
            record.order,
        )

    def _record_degenerate(self, pid, primitive):
        if not _is_degenerate(primitive.verts):
            self._degenerate.discard(pid)
            return False
        if pid not in self._degenerate:
            self._pruned_count += 1
            self._degenerate.add(pid)
        return True

    def _effective_states(self, selected):
        selected_set = set(selected)
        latest = self._snapshots.get(self._latest_revision)
        published = latest.states if latest is not None else {}
        states = {}
        for pid, record in self._records.items():
            if pid in self._removed:
                continue
            if pid not in selected_set and pid in self._dirty:
                if pid in published:
                    states[pid] = published[pid]
                continue
            primitive = self._make_primitive(record)
            if self._record_degenerate(pid, primitive):
                continue
            states[pid] = primitive
        return states

    def refit(self, elapsed_ms=0.0):
        with self._lock:
            try:
                measured_elapsed = float(elapsed_ms)
            except (TypeError, ValueError) as exc:
                raise ValueError("elapsed_ms must be numeric") from exc
            if not math.isfinite(measured_elapsed) or measured_elapsed < 0.0:
                raise ValueError("elapsed_ms must be finite and non-negative")

            dirty_count = len(self._dirty)
            selected, over_budget = self._select_dirty(measured_elapsed)

            latest = self._snapshots.get(self._latest_revision)
            updated_nodes = set()
            if latest is not None:
                for pid in selected:
                    updated_nodes.update(latest.leaf_paths.get(pid, ()))

            states = self._effective_states(selected)
            revision = self._latest_revision + 1
            snapshot = self._make_snapshot(revision, states)
            self._store_snapshot(snapshot)

            for pid in selected:
                self._dirty.pop(pid, None)

            self._dirty_nodes.clear()
            for pid in self._dirty:
                self._mark_ancestors(pid)

            updated = len(selected)
            skipped = dirty_count - updated
            self._last_updated_nodes = len(updated_nodes)
            degraded = over_budget or skipped > 0
            return {
                "revision": revision,
                "updated": updated,
                "dirty": dirty_count,
                "skipped": skipped,
                "degraded": degraded,
                "updated_ids": selected,
                "updated_nodes": len(updated_nodes),
                "elapsed_ms": measured_elapsed,
            }

    def _ray_box_interval(self, origin, direction, bounds):
        t_near = 0.0
        t_far = _INF
        for axis in range(3):
            lower = bounds[0][axis]
            upper = bounds[1][axis]
            if abs(direction[axis]) <= _EPS:
                if origin[axis] < lower or origin[axis] > upper:
                    return None
                continue
            inverse = 1.0 / direction[axis]
            first = (lower - origin[axis]) * inverse
            second = (upper - origin[axis]) * inverse
            if first > second:
                first, second = second, first
            if first > t_near:
                t_near = first
            if second < t_far:
                t_far = second
            if t_far < t_near:
                return None
        if t_far < 0.0:
            return None
        return t_near, t_far

    def _ray_triangle_interval(self, origin, direction, primitive):
        verts = primitive.verts
        translate = primitive.translate
        vertex_a = (
            verts[0][0] + translate[0],
            verts[0][1] + translate[1],
            verts[0][2] + translate[2],
        )
        vertex_b = (
            verts[1][0] + translate[0],
            verts[1][1] + translate[1],
            verts[1][2] + translate[2],
        )
        vertex_c = (
            verts[2][0] + translate[0],
            verts[2][1] + translate[1],
            verts[2][2] + translate[2],
        )
        edge_a = _sub(vertex_b, vertex_a)
        edge_b = _sub(vertex_c, vertex_a)
        pvec = _cross(direction, edge_b)
        determinant = _dot(edge_a, pvec)
        direction_squared = _dot(direction, direction)
        edge_product = math.sqrt(
            max(0.0, _dot(edge_a, edge_a))
            * max(0.0, _dot(edge_b, edge_b))
        )
        determinant_tolerance = max(
            1e-300, 1e-12 * math.sqrt(direction_squared) * edge_product
        )
        if abs(determinant) <= determinant_tolerance:
            return None
        inverse = 1.0 / determinant
        tvec = _sub(origin, vertex_a)
        u = _dot(tvec, pvec) * inverse
        if u < 0.0 or u > 1.0:
            return None
        qvec = _cross(tvec, edge_a)
        v = _dot(direction, qvec) * inverse
        if v < 0.0 or u + v > 1.0:
            return None
        distance = _dot(edge_b, qvec) * inverse
        if distance <= _HIT_EPS:
            return None
        return distance

    def query(self, origin, direction, revision=None):
        ray_origin = _as_vec3(origin, "origin")
        ray_direction = _as_vec3(direction, "direction")
        if ray_direction == (0.0, 0.0, 0.0):
            return []

        with self._lock:
            if revision is None:
                revision = self._latest_revision
                removed = set(self._removed)
            else:
                removed = set()
            snapshot = self._snapshots.get(revision)
            if snapshot is None:
                raise KeyError(revision)
            root = snapshot.root

        if root is None:
            return []

        hits = []
        stack = [root]
        while stack:
            node = stack.pop()
            interval = self._ray_box_interval(ray_origin, ray_direction, node.bounds)
            if interval is None:
                continue
            if node.left is None and node.right is None:
                for primitive in node.primitives:
                    if primitive.id in removed:
                        continue
                    distance = self._ray_triangle_interval(
                        ray_origin, ray_direction, primitive
                    )
                    if distance is not None:
                        hits.append({"id": primitive.id, "t": distance})
            else:
                if node.left is not None:
                    stack.append(node.left)
                if node.right is not None:
                    stack.append(node.right)

        hits.sort(key=lambda hit: (hit["t"], repr(hit["id"])))
        return hits

    def remove(self, pid):
        with self._lock:
            if pid not in self._records:
                raise KeyError(pid)
            del self._records[pid]
            self._dirty.pop(pid, None)
            self._removed.add(pid)
            self._degenerate.discard(pid)

    def compact(self):
        start = time.perf_counter()
        with self._lock:
            freed = len(self._removed)
            latest = self._snapshots.get(self._latest_revision)

            states = {}
            for pid, record in self._records.items():
                if pid in self._removed:
                    continue
                if pid in self._dirty:
                    if latest is not None and pid in latest.states:
                        states[pid] = latest.states[pid]
                    continue
                if latest is not None and pid in latest.states:
                    states[pid] = latest.states[pid]
                    continue
                primitive = self._make_primitive(record)
                if not self._record_degenerate(pid, primitive):
                    states[pid] = primitive

            old_node_count = latest.node_count if latest is not None else 0
            revision = self._latest_revision + 1
            snapshot = self._make_snapshot(revision, states)
            self._snapshots.clear()
            self._latest_revision = revision
            self._store_snapshot(snapshot)
            self._removed.clear()
            self._dirty_nodes.clear()
            for pid in self._dirty:
                self._mark_ancestors(pid)

            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return {
                "revision": revision,
                "freed": freed,
                "remaining": len(self._records),
                "compressed_nodes": max(0, old_node_count - snapshot.node_count),
                "node_count": snapshot.node_count,
                "elapsed_ms": elapsed_ms,
            }

    def stats(self):
        with self._lock:
            snapshot = self._snapshots.get(self._latest_revision)
            return {
                "revision": self._latest_revision,
                "primitive_count": len(self._records)
                - len(self._degenerate.intersection(self._records)),
                "node_count": snapshot.node_count if snapshot is not None else 0,
                "pruned": self._pruned_count,
                "dirty": len(self._dirty),
                "removed": len(self._removed),
                "updated_nodes": self._last_updated_nodes,
            }

    def root_bounds(self, revision=None):
        with self._lock:
            if revision is None:
                revision = self._latest_revision
            snapshot = self._snapshots.get(revision)
            if snapshot is None:
                raise KeyError(revision)
            return snapshot.bounds()
