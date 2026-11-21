"""Metaball field evaluation and marching-cubes polygonization.

Contract used by the tests
--------------------------
* Field of one ball (center c, radius r, strength s):
  ``s * (1 - |p - c|^2 / r^2)`` when ``|p - c| < r``, otherwise 0.
  The field is the sum over all balls.
* ``add_ball`` rejects a radius that is not a finite number > 0 and a center
  or strength that is not finite; it returns the ball index.
* ``iso`` defaults to 0.5.  A point is inside when
  ``field > iso + tolerance``; values inside the tolerance band around iso
  are consistently treated as outside, so boundary thresholds are stable and
  a near-zero threshold still produces a mesh.
* ``polygonize(bounds_min, bounds_max)`` returns
  ``{"vertices", "indices", "normals", "stats"}``.
* Vertices on shared edges are welded; the vertex list contains no
  duplicates.  Indices are non-negative ints inside range and a multiple of
  three in count.
* Normals come from central differences of ``field`` and are unit length,
  pointing outwards.
* Adaptive depth: neighbouring cells differ by at most one level;
  ``stats["max_depth_delta"]`` reports that maximum.
* ``stats`` is ``{"balls", "vertices", "indices", "cells",
  "max_depth_delta", "estimated_ms", "truncated"}``.  Each cell sample costs
  ``sample_cost_ms`` (default 0.002) and ``estimated_ms`` must stay within
  ``budget_ms``; the depth is reduced instead of truncating the mesh, so
  ``truncated`` is always False.
* Repeated calls on one instance return identical geometry, and concurrent
  calls are deterministic.
"""

import math
import threading


_EPSILON = 1e-12


def _as_finite_tuple(value, name):
    """Return *value* as a finite 3-tuple, or raise ``ValueError``."""
    try:
        result = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be an iterable of three numbers" % name) from exc
    if len(result) != 3:
        raise ValueError("%s must contain exactly three numbers" % name)
    if not all(math.isfinite(component) for component in result):
        raise ValueError("%s must contain only finite numbers" % name)
    return result


class MetaballMarch:
    """A deterministic, budget-aware metaball polygonizer.

    The public state is intentionally kept small: balls are stored as an
    immutable snapshot, while all temporary caches belong to one
    ``polygonize`` call.  That makes concurrent calls independent and avoids
    the classic shared-cache race that produces duplicate seam vertices.
    """

    def __init__(self, iso=0.5, max_depth=3, budget_ms=8.0, tolerance=1e-6, sample_cost_ms=0.002):
        self.iso = self._finite_float(iso, "iso")
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise ValueError("max_depth must be a non-negative integer")
        self.max_depth = max_depth
        self.budget_ms = self._non_negative_float(budget_ms, "budget_ms")
        self.tolerance = self._non_negative_float(tolerance, "tolerance")
        self.sample_cost_ms = self._non_negative_float(sample_cost_ms, "sample_cost_ms")

        self._lock = threading.RLock()
        self._balls = ()
        self._last_stats = {
            "balls": 0,
            "vertices": 0,
            "indices": 0,
            "cells": 0,
            "max_depth_delta": 0,
            "estimated_ms": 0.0,
            "truncated": False,
        }

    @staticmethod
    def _finite_float(value, name):
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be a finite number" % name) from exc
        if not math.isfinite(result):
            raise ValueError("%s must be a finite number" % name)
        return result

    @classmethod
    def _non_negative_float(cls, value, name):
        result = cls._finite_float(value, name)
        if result < 0.0:
            raise ValueError("%s must be non-negative" % name)
        return result

    def add_ball(self, center, radius, strength=1.0):
        """Append a ball and return its index.

        Invalid geometry is rejected at the API boundary so NaNs cannot reach
        the field, the polygonizer, or the caller's bounding box.
        """
        center = _as_finite_tuple(center, "center")
        radius = self._finite_float(radius, "radius")
        strength = self._finite_float(strength, "strength")
        if radius <= 0.0:
            raise ValueError("radius must be greater than zero")

        with self._lock:
            index = len(self._balls)
            self._balls = self._balls + ((center, radius, strength),)
        return index

    @staticmethod
    def _evaluate(point, balls):
        x, y, z = point
        total = 0.0
        for (cx, cy, cz), radius, strength in balls:
            dx = x - cx
            dy = y - cy
            dz = z - cz
            distance_squared = dx * dx + dy * dy + dz * dz
            radius_squared = radius * radius
            if radius_squared == 0.0:
                if distance_squared == 0.0:
                    total += strength
                continue
            if distance_squared < radius_squared:
                total += strength * (1.0 - distance_squared / radius_squared)
        return total

    def field(self, point):
        """Evaluate the summed metaball field at *point*."""
        point = _as_finite_tuple(point, "point")
        return self._evaluate(point, self._balls)

    def _sample(self, point, balls, cache):
        value = cache.get(point)
        if value is None:
            value = self._evaluate(point, balls)
            cache[point] = value
        return value

    def _normal(self, point, balls, cache, step, fallback_origin):
        x, y, z = point
        xp = (x + step, y, z)
        xm = (x - step, y, z)
        yp = (x, y + step, z)
        ym = (x, y - step, z)
        zp = (x, y, z + step)
        zm = (x, y, z - step)

        gx = (self._sample(xp, balls, cache) - self._sample(xm, balls, cache)) / (2.0 * step)
        gy = (self._sample(yp, balls, cache) - self._sample(ym, balls, cache)) / (2.0 * step)
        gz = (self._sample(zp, balls, cache) - self._sample(zm, balls, cache)) / (2.0 * step)
        length = math.sqrt(gx * gx + gy * gy + gz * gz)
        if length > _EPSILON and math.isfinite(length):
            return (-gx / length, -gy / length, -gz / length)

        fx = 0.0
        fy = 0.0
        fz = 0.0
        for (cx, cy, cz), radius, strength in balls:
            dx = x - cx
            dy = y - cy
            dz = z - cz
            distance_squared = dx * dx + dy * dy + dz * dz
            radius_squared = radius * radius
            if distance_squared < radius_squared:
                weight = abs(strength) * (1.0 - distance_squared / radius_squared)
                fx += weight * dx
                fy += weight * dy
                fz += weight * dz
        if fx == 0.0 and fy == 0.0 and fz == 0.0:
            fx = x - fallback_origin[0]
            fy = y - fallback_origin[1]
            fz = z - fallback_origin[2]
        length = math.sqrt(fx * fx + fy * fy + fz * fz)
        if length > _EPSILON and math.isfinite(length):
            return (fx / length, fy / length, fz / length)
        return (1.0, 0.0, 0.0)

    @staticmethod
    def _tetrahedra():
        """Return a conforming Kuhn/Freudenthal decomposition."""
        return (
            (0, 1, 3, 7),
            (0, 1, 5, 7),
            (0, 2, 3, 7),
            (0, 2, 6, 7),
            (0, 4, 5, 7),
            (0, 4, 6, 7),
        )

    def _depth_for_budget(self, max_depth, budget_ms):
        if self.sample_cost_ms <= 0.0:
            return max_depth
        affordable_cells = int(math.floor(budget_ms / self.sample_cost_ms + 1e-9))
        depth = 0
        while depth < max_depth:
            next_side = 1 << (depth + 1)
            if next_side ** 3 > affordable_cells:
                break
            depth += 1
        return depth

    def _generate(self, bounds_min, bounds_max, depth, balls):
        n = 1 << depth
        side = n + 1
        x0, y0, z0 = bounds_min
        x1, y1, z1 = bounds_max
        dx = (x1 - x0) / n
        dy = (y1 - y0) / n
        dz = (z1 - z0) / n

        xs = [x0 + index * dx for index in range(side)]
        ys = [y0 + index * dy for index in range(side)]
        zs = [z0 + index * dz for index in range(side)]

        iso_limit = self.iso + self.tolerance
        fallback_origin = (
            (x0 + x1) * 0.5,
            (y0 + y1) * 0.5,
            (z0 + z1) * 0.5,
        )
        normal_step = 0.125 * min(abs(dx), abs(dy), abs(dz))
        if normal_step == 0.0 or not math.isfinite(normal_step):
            normal_step = 1e-12 * max(1.0, abs(x1 - x0), abs(y1 - y0), abs(z1 - z0))

        sample_cache = {}
        values = [0.0] * (side ** 3)
        for i, x in enumerate(xs):
            for j, y in enumerate(ys):
                base = (i * side + j) * side
                for k, z in enumerate(zs):
                    point = (x, y, z)
                    value = self._evaluate(point, balls)
                    sample_cache[point] = value
                    values[base + k] = value

        vertices = []
        normals = []
        indices = []
        edge_cache = {}
        position_cache = {}

        def corner_id(i, j, k):
            return (i * side + j) * side + k

        def corner_point(corner):
            i, remainder = divmod(corner, side * side)
            j, k = divmod(remainder, side)
            return xs[i], ys[j], zs[k]

        def corner_value(corner):
            return values[corner]

        def edge_vertex(first, second):
            if first > second:
                first, second = second, first
            key = (first, second)
            cached = edge_cache.get(key)
            if cached is not None:
                return cached

            point_a = corner_point(first)
            point_b = corner_point(second)
            value_a = corner_value(first)
            value_b = corner_value(second)
            denominator = value_b - value_a
            if denominator == 0.0 or not math.isfinite(denominator):
                amount = 0.5
            else:
                amount = (self.iso - value_a) / denominator
                if not math.isfinite(amount):
                    amount = 0.5
                elif amount < 0.0:
                    amount = 0.0
                elif amount > 1.0:
                    amount = 1.0

            position = (
                point_a[0] + amount * (point_b[0] - point_a[0]),
                point_a[1] + amount * (point_b[1] - point_a[1]),
                point_a[2] + amount * (point_b[2] - point_a[2]),
            )

            vertex_index = position_cache.get(position)
            if vertex_index is None:
                vertex_index = len(vertices)
                vertices.append(position)
                normals.append(
                    self._normal(position, balls, sample_cache, normal_step, fallback_origin)
                )
                position_cache[position] = vertex_index
            edge_cache[key] = vertex_index
            return vertex_index

        def add_triangle(a, b, c):
            if a == b or b == c or a == c:
                return
            pa = vertices[a]
            pb = vertices[b]
            pc = vertices[c]
            abx = pb[0] - pa[0]
            aby = pb[1] - pa[1]
            abz = pb[2] - pa[2]
            acx = pc[0] - pa[0]
            acy = pc[1] - pa[1]
            acz = pc[2] - pa[2]
            nx = aby * acz - abz * acy
            ny = abz * acx - abx * acz
            nz = abx * acy - aby * acx
            area_squared = nx * nx + ny * ny + nz * nz
            if area_squared == 0.0 or not math.isfinite(area_squared):
                return
            average = (
                normals[a][0] + normals[b][0] + normals[c][0],
                normals[a][1] + normals[b][1] + normals[c][1],
                normals[a][2] + normals[b][2] + normals[c][2],
            )
            if nx * average[0] + ny * average[1] + nz * average[2] < 0.0:
                b, c = c, b
            indices.extend((a, b, c))

        tets = self._tetrahedra()
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    cell_corners = [0] * 8
                    for local in range(8):
                        li = i + (local & 1)
                        lj = j + ((local >> 1) & 1)
                        lk = k + ((local >> 2) & 1)
                        cell_corners[local] = corner_id(li, lj, lk)

                    for tet in tets:
                        ids = [cell_corners[local] for local in tet]
                        inside = [
                            index
                            for index, corner in enumerate(ids)
                            if corner_value(corner) > iso_limit
                        ]

                        if len(inside) == 1:
                            a = inside[0]
                            outside = [index for index in range(4) if index != a]
                            triangle = []
                            for b in outside:
                                vertex = edge_vertex(ids[a], ids[b])
                                triangle.append(vertex)
                            add_triangle(triangle[0], triangle[1], triangle[2])
                        elif len(inside) == 3:
                            a = next(index for index in range(4) if index not in inside)
                            triangle = [
                                edge_vertex(ids[a], ids[b])
                                for b in inside
                            ]
                            add_triangle(triangle[0], triangle[1], triangle[2])
                        elif len(inside) == 2:
                            a, b = inside
                            outside = [index for index in range(4) if index not in inside]
                            c, d = outside
                            ac = edge_vertex(ids[a], ids[c])
                            ad = edge_vertex(ids[a], ids[d])
                            bd = edge_vertex(ids[b], ids[d])
                            bc = edge_vertex(ids[b], ids[c])
                            add_triangle(ac, ad, bd)
                            add_triangle(ac, bd, bc)

        cells = n ** 3
        stats = {
            "balls": len(balls),
            "vertices": len(vertices),
            "indices": len(indices),
            "cells": cells,
            "max_depth_delta": 0,
            "estimated_ms": cells * self.sample_cost_ms,
            "truncated": False,
        }
        return {
            "vertices": vertices,
            "indices": indices,
            "normals": normals,
            "stats": stats,
        }

    def polygonize(self, bounds_min, bounds_max, budget_ms=None):
        """Polygonize the current balls inside an axis-aligned box.

        The generated mesh is always complete.  When the sample budget is
        tight, the global grid depth is lowered instead of dropping trailing
        cells or triangles.
        """
        bounds_min = _as_finite_tuple(bounds_min, "bounds_min")
        bounds_max = _as_finite_tuple(bounds_max, "bounds_max")
        if any(high <= low for low, high in zip(bounds_min, bounds_max)):
            raise ValueError("bounds_max must be greater than bounds_min on every axis")

        if budget_ms is None:
            budget_ms = self.budget_ms
        else:
            budget_ms = self._non_negative_float(budget_ms, "budget_ms")

        balls = self._balls
        depth = self._depth_for_budget(self.max_depth, budget_ms)
        result = self._generate(bounds_min, bounds_max, depth, balls)

        with self._lock:
            self._last_stats = dict(result["stats"])
        return result

    def stats(self):
        """Return a copy of the statistics from the latest polygonization."""
        with self._lock:
            return dict(self._last_stats)
