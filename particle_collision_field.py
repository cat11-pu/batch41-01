"""Particle collision field with continuous (swept) detection.

Public API::

    ParticleCollisionField(triangles=None, materials=None, cell_size=None,
                           max_radius=None, budget_ms=2.0, max_substeps=64)
        .add_triangle(a, b, c, material="default") -> int
        .add_particle(pid, position, velocity, radius,
                      material="default", priority=0)
        .remove_particle(pid)
        .normal_at(triangle_id, point) -> (nx, ny, nz)
        .cell_size -> float
        .step(dt, elapsed_ms=0.0, workers=1) -> dict
        .stats() -> dict

The implementation uses a swept-sphere test in each velocity-derived
substep.  Particles are spatially hashed; the particle hash is rebuilt into a
fresh dictionary and published atomically (a double buffer), so concurrent
readers never observe a partially built bucket chain.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict


_EPS = 1e-12
_DEFAULT_FRICTION = 0.5
_DEFAULT_RESTITUTION = 0.2


def _as_vec(value, name):
    try:
        vector = tuple(float(component) for component in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a three-element numeric vector" % name) from exc
    if len(vector) != 3:
        raise ValueError("%s must be a three-element numeric vector" % name)
    if not all(math.isfinite(component) for component in vector):
        raise ValueError("%s must contain only finite values" % name)
    return vector


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a, scale):
    return (a[0] * scale, a[1] * scale, a[2] * scale)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(a):
    return math.sqrt(_dot(a, a))


def _normalize(a):
    length = _length(a)
    if length <= _EPS:
        return None
    return _mul(a, 1.0 / length)


def _pid_key(pid):
    if isinstance(pid, (int, float)) and not isinstance(pid, bool):
        return (0, float(pid), "")
    return (1, 0.0, str(pid))


def _quantized(value):
    # Edge ownership must not depend on floating point noise introduced by
    # meshes exported from different tools.
    return round(float(value), 9)


def _edge_key(a, b):
    va = tuple(_quantized(component) for component in a)
    vb = tuple(_quantized(component) for component in b)
    return (va, vb) if va <= vb else (vb, va)


def _cell_coord(value, cell_size):
    return int(math.floor(value / cell_size))


class _Triangle:
    __slots__ = ("a", "b", "c", "normal", "material", "edges", "bbox")

    def __init__(self, a, b, c, material):
        self.a = a
        self.b = b
        self.c = c
        normal = _normalize(_cross(_sub(b, a), _sub(c, a)))
        if normal is None:
            raise ValueError("triangle vertices are collinear or degenerate")
        self.normal = normal
        self.material = material
        self.edges = (
            _edge_key(a, b),
            _edge_key(b, c),
            _edge_key(c, a),
        )
        self.bbox = (
            (min(a[0], b[0], c[0]), min(a[1], b[1], c[1]), min(a[2], b[2], c[2])),
            (max(a[0], b[0], c[0]), max(a[1], b[1], c[1]), max(a[2], b[2], c[2])),
        )


class _Particle:
    __slots__ = (
        "pid", "position", "velocity", "radius", "material", "priority",
    )

    def __init__(self, pid, position, velocity, radius, material, priority):
        self.pid = pid
        self.position = position
        self.velocity = velocity
        self.radius = radius
        self.material = material
        self.priority = priority


class ParticleCollisionField:
    def __init__(self, triangles=None, materials=None, cell_size=None,
                 max_radius=None, budget_ms=2.0, max_substeps=64):
        self._triangles = []
        self._edge_triangles = defaultdict(list)
        self._materials = dict(materials or {})
        self._requested_cell_size = self._positive_float_or_none(cell_size)
        self._max_radius = self._positive_float_or_none(max_radius)
        self.budget_ms = float(budget_ms)
        self.max_substeps = max(1, int(max_substeps))

        self._particles = {}
        self._particle_hash = {}
        self._triangle_hash = {}
        self._hash_lock = threading.RLock()
        self._bucket_locks = {}

        for triangle in triangles or ():
            if isinstance(triangle, dict):
                self.add_triangle(
                    triangle["a"], triangle["b"], triangle["c"],
                    material=triangle.get("material", "default"),
                )
            else:
                self.add_triangle(*triangle)

    @staticmethod
    def _positive_float_or_none(value):
        if value is None:
            return None
        value = float(value)
        if not math.isfinite(value) or value <= 0.0:
            return None
        return value

    @property
    def cell_size(self):
        actual_max = max(
            (particle.radius for particle in self._particles.values()),
            default=0.0,
        )
        if self._max_radius is not None:
            actual_max = max(actual_max, self._max_radius)

        required = 2.0 * actual_max
        requested = self._requested_cell_size
        if requested is None:
            requested = required
        size = max(requested, required)
        if not math.isfinite(size) or size <= 0.0:
            size = 1.0
        return size

    def add_triangle(self, a, b, c, material="default"):
        a = _as_vec(a, "a")
        b = _as_vec(b, "b")
        c = _as_vec(c, "c")
        triangle = _Triangle(a, b, c, material)
        triangle_id = len(self._triangles)
        self._triangles.append(triangle)
        for edge in triangle.edges:
            self._edge_triangles[edge].append(triangle_id)
        self._build_triangle_hash()
        return triangle_id

    def add_particle(self, pid, position, velocity, radius,
                     material="default", priority=0):
        position = _as_vec(position, "position")
        velocity = _as_vec(velocity, "velocity")
        try:
            radius = float(radius)
        except (TypeError, ValueError) as exc:
            raise ValueError("radius must be a finite positive number") from exc
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError("radius must be a finite positive number")
        try:
            priority = int(priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("priority must be an integer") from exc

        with self._hash_lock:
            self._particles[pid] = _Particle(
                pid, position, velocity, radius, material, priority,
            )
            self._rebuild_particle_hash_locked()
        return pid

    def remove_particle(self, pid):
        with self._hash_lock:
            existed = self._particles.pop(pid, None) is not None
            if existed:
                self._rebuild_particle_hash_locked()
        return existed

    def _point_on_segment(self, point, a, b):
        ab = _sub(b, a)
        length_sq = _dot(ab, ab)
        if length_sq <= _EPS:
            return _length(_sub(point, a)) <= 1e-6
        t = _dot(_sub(point, a), ab) / length_sq
        if t < -1e-6 or t > 1.0 + 1e-6:
            return False
        closest = _add(a, _mul(ab, min(1.0, max(0.0, t))))
        return _length(_sub(point, closest)) <= 1e-6

    def normal_at(self, triangle_id, point):
        triangle = self._triangles[int(triangle_id)]
        point = _as_vec(point, "point")

        adjacent = set()
        for edge in triangle.edges:
            a, b = edge
            if self._point_on_segment(point, a, b):
                adjacent.update(self._edge_triangles.get(edge, ()))

        if len(adjacent) > 1:
            total = (0.0, 0.0, 0.0)
            for tid in sorted(adjacent):
                total = _add(total, self._triangles[tid].normal)
            smoothed = _normalize(total)
            if smoothed is not None:
                return smoothed
        return triangle.normal

    def _build_triangle_hash(self):
        cell_size = self.cell_size
        buckets = defaultdict(list)
        for triangle_id, triangle in enumerate(self._triangles):
            low, high = triangle.bbox
            x0 = _cell_coord(low[0], cell_size)
            y0 = _cell_coord(low[1], cell_size)
            z0 = _cell_coord(low[2], cell_size)
            x1 = _cell_coord(high[0], cell_size)
            y1 = _cell_coord(high[1], cell_size)
            z1 = _cell_coord(high[2], cell_size)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    for z in range(z0, z1 + 1):
                        buckets[(x, y, z)].append(triangle_id)
        self._triangle_hash = {key: tuple(ids) for key, ids in buckets.items()}

    def _rebuild_particle_hash_locked(self):
        cell_size = self.cell_size
        new_hash = {}
        for pid in sorted(self._particles, key=_pid_key):
            particle = self._particles[pid]
            key = tuple(
                _cell_coord(component, cell_size)
                for component in particle.position
            )
            new_hash.setdefault(key, []).append(pid)
        # Publish a fully built mapping in one operation.  Readers either see
        # the old complete hash or the new complete hash, never a broken chain.
        self._particle_hash = {
            key: tuple(pids) for key, pids in new_hash.items()
        }

    def _particle_hash_from(self, particles):
        cell_size = self.cell_size
        buckets = defaultdict(list)
        for pid in sorted(particles, key=_pid_key):
            position = particles[pid].position
            key = tuple(_cell_coord(component, cell_size) for component in position)
            buckets[key].append(pid)
        return {key: tuple(pids) for key, pids in buckets.items()}

    def _triangle_candidates_for_segment(self, start, end, radius):
        cell_size = self.cell_size
        low = (
            min(start[0], end[0]) - radius,
            min(start[1], end[1]) - radius,
            min(start[2], end[2]) - radius,
        )
        high = (
            max(start[0], end[0]) + radius,
            max(start[1], end[1]) + radius,
            max(start[2], end[2]) + radius,
        )
        x0, y0, z0 = (_cell_coord(value, cell_size) for value in low)
        x1, y1, z1 = (_cell_coord(value, cell_size) for value in high)
        found = set()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    found.update(self._triangle_hash.get((x, y, z), ()))
        return sorted(found)

    @staticmethod
    def _point_in_triangle(point, a, b, c):
        v0 = _sub(b, a)
        v1 = _sub(c, a)
        v2 = _sub(point, a)
        dot00 = _dot(v0, v0)
        dot01 = _dot(v0, v1)
        dot02 = _dot(v0, v2)
        dot11 = _dot(v1, v1)
        dot12 = _dot(v1, v2)
        denom = dot00 * dot11 - dot01 * dot01
        if abs(denom) <= _EPS:
            return False
        u = (dot11 * dot02 - dot01 * dot12) / denom
        v = (dot00 * dot12 - dot01 * dot02) / denom
        tolerance = 1e-7
        return (
            u >= -tolerance
            and v >= -tolerance
            and u + v <= 1.0 + tolerance
        )

    def _sweep_triangle(self, start, end, radius, triangle_id):
        triangle = self._triangles[triangle_id]
        normal = triangle.normal
        start_distance = _dot(_sub(start, triangle.a), normal)
        end_distance = _dot(_sub(end, triangle.a), normal)
        tolerance = 1e-9

        if start_distance < -radius - tolerance:
            return None

        if start_distance > radius + tolerance:
            denominator = start_distance - end_distance
            if denominator <= tolerance or end_distance > radius - tolerance:
                return None
            fraction = (start_distance - radius) / denominator
        else:
            approaching = end_distance < start_distance - tolerance
            if start_distance >= radius - tolerance and not approaching:
                return None
            fraction = 0.0

        fraction = min(1.0, max(0.0, fraction))
        center_at_hit = _add(start, _mul(_sub(end, start), fraction))
        plane_distance = _dot(_sub(center_at_hit, triangle.a), normal)
        surface_point = _add(center_at_hit, _mul(normal, -plane_distance))
        if not self._point_in_triangle(
            surface_point, triangle.a, triangle.b, triangle.c,
        ):
            return None
        return fraction, surface_point

    def _existing_material(self, name):
        entry = self._materials.get(name)
        fallback = False
        if not isinstance(entry, dict):
            entry = {}
            fallback = True

        friction = entry.get("friction")
        restitution = entry.get("restitution")

        if friction is None:
            fallback = True
            friction = _DEFAULT_FRICTION
        else:
            try:
                friction = float(friction)
            except (TypeError, ValueError):
                friction = float("nan")
            if not math.isfinite(friction):
                fallback = True
                friction = _DEFAULT_FRICTION
            else:
                friction = min(1.0, max(0.0, friction))

        if restitution is None:
            fallback = True
            restitution = _DEFAULT_RESTITUTION
        else:
            try:
                restitution = float(restitution)
            except (TypeError, ValueError):
                restitution = float("nan")
            if not math.isfinite(restitution):
                fallback = True
                restitution = _DEFAULT_RESTITUTION
            else:
                restitution = min(1.0, max(0.0, restitution))

        return friction, restitution, fallback

    def _contact_material(self, triangle_id, particle):
        triangle_friction, triangle_restitution, triangle_fallback = (
            self._existing_material(self._triangles[triangle_id].material)
        )
        particle_friction, particle_restitution, particle_fallback = (
            self._existing_material(particle.material)
        )
        if triangle_fallback:
            friction = particle_friction
            restitution = particle_restitution
        else:
            friction = triangle_friction
            restitution = triangle_restitution
        return friction, restitution, int(triangle_fallback) + int(particle_fallback)

    def _resolve_particle(self, particle, dt_substep, allowed_surface):
        position = particle.position
        velocity = particle.velocity
        radius = particle.radius
        remaining = 1.0
        contacts = 0
        fallback_count = 0

        for _ in range(8):
            if remaining <= _EPS:
                break
            displacement = _mul(velocity, dt_substep * remaining)
            end = _add(position, displacement)
            best = None
            for triangle_id in self._triangle_candidates_for_segment(
                position, end, radius,
            ):
                if (
                    allowed_surface is not None
                    and (particle.pid, triangle_id) not in allowed_surface
                ):
                    continue
                hit = self._sweep_triangle(
                    position, end, radius, triangle_id,
                )
                if hit is None:
                    continue
                fraction, surface_point = hit
                candidate = (fraction, triangle_id, surface_point)
                if (
                    best is None
                    or fraction < best[0] - 1e-12
                    or (
                        abs(fraction - best[0]) <= 1e-12
                        and triangle_id < best[1]
                    )
                ):
                    best = candidate

            if best is None:
                position = end
                break

            fraction, triangle_id, surface_point = best
            position = _add(position, _mul(displacement, fraction))
            triangle = self._triangles[triangle_id]
            normal = self.normal_at(triangle_id, surface_point)

            # Correct penetration along the smoothed contact normal.  Use the
            # face plane as a floor so smoothing cannot leave the centre
            # inside the actual triangle.
            face_gap = radius - _dot(_sub(position, triangle.a), triangle.normal)
            if face_gap > 0.0:
                position = _add(position, _mul(triangle.normal, face_gap))
            smooth_gap = radius - _dot(_sub(position, triangle.a), normal)
            if smooth_gap > 0.0:
                position = _add(position, _mul(normal, smooth_gap))

            normal_speed = _dot(velocity, normal)
            if normal_speed < 0.0:
                friction, restitution, fallbacks = self._contact_material(
                    triangle_id, particle,
                )
                fallback_count += fallbacks
                velocity = _add(
                    velocity, _mul(normal, -(1.0 + restitution) * normal_speed),
                )
                normal_speed = _dot(velocity, normal)
                tangent = _sub(velocity, _mul(normal, normal_speed))
                velocity = _add(
                    _mul(normal, normal_speed), _mul(tangent, 1.0 - friction),
                )
                contacts += 1

            remaining *= max(0.0, 1.0 - fraction)

        particle.position = position
        particle.velocity = velocity
        return contacts, fallback_count

    def _particle_pair_candidates(self, particles):
        if len(particles) < 2:
            return []
        cell_size = self.cell_size
        max_radius = max(particle.radius for particle in particles.values())
        buckets = self._particle_hash_from(particles)
        seen = set()
        candidates = []

        for pid in sorted(particles, key=_pid_key):
            particle = particles[pid]
            x, y, z = particle.position
            x0 = _cell_coord(x - max_radius, cell_size)
            y0 = _cell_coord(y - max_radius, cell_size)
            z0 = _cell_coord(z - max_radius, cell_size)
            x1 = _cell_coord(x + max_radius, cell_size)
            y1 = _cell_coord(y + max_radius, cell_size)
            z1 = _cell_coord(z + max_radius, cell_size)
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    for cz in range(z0, z1 + 1):
                        for other_pid in buckets.get((cx, cy, cz), ()):
                            if other_pid == pid:
                                continue
                            pair = tuple(sorted(
                                (pid, other_pid), key=_pid_key,
                            ))
                            if pair in seen:
                                continue
                            seen.add(pair)
                            other = particles[other_pid]
                            distance = _length(_sub(
                                particle.position, other.position,
                            ))
                            if distance <= particle.radius + other.radius + 1e-9:
                                candidates.append({
                                    "kind": "particle",
                                    "pids": pair,
                                    "priority": max(
                                        particle.priority, other.priority,
                                    ),
                                    "triangle_id": None,
                                })
        return candidates

    def _surface_candidates(self, particles, dt):
        candidates = []
        seen = set()
        for pid in sorted(particles, key=_pid_key):
            particle = particles[pid]
            end = _add(particle.position, _mul(particle.velocity, dt))
            for triangle_id in self._triangle_candidates_for_segment(
                particle.position, end, particle.radius,
            ):
                key = (pid, triangle_id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "kind": "surface",
                    "pids": (pid,),
                    "priority": particle.priority,
                    "triangle_id": triangle_id,
                })
        return candidates

    @staticmethod
    def _candidate_sort_key(candidate):
        pids = candidate["pids"]
        return (
            -candidate["priority"],
            tuple(_pid_key(pid) for pid in pids),
            candidate["kind"],
            -1 if candidate["triangle_id"] is None else candidate["triangle_id"],
        )

    def _substep_count(self, particles, dt):
        if abs(dt) <= _EPS or not particles:
            return 1
        needed = 1
        for particle in particles.values():
            speed = _length(particle.velocity)
            if speed <= _EPS:
                continue
            travel = speed * abs(dt)
            steps = int(math.ceil(travel / max(particle.radius, 1e-6)))
            needed = max(needed, steps)
        return max(1, min(needed, self.max_substeps))

    def step(self, dt, elapsed_ms=0.0, workers=1):
        try:
            dt = float(dt)
            elapsed_ms = float(elapsed_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("dt and elapsed_ms must be finite numbers") from exc
        if not math.isfinite(dt) or not math.isfinite(elapsed_ms):
            raise ValueError("dt and elapsed_ms must be finite numbers")
        if dt < 0.0:
            raise ValueError("dt must not be negative")

        # Deterministic snapshot.  `workers` is accepted for API compatibility;
        # all writes are deterministic and the shared hash is double buffered,
        # so parallel callers cannot produce different trajectories.
        with self._hash_lock:
            particles = {
                pid: _Particle(
                    particle.pid,
                    particle.position,
                    particle.velocity,
                    particle.radius,
                    particle.material,
                    particle.priority,
                )
                for pid, particle in self._particles.items()
            }
            self._build_triangle_hash()

        substeps = self._substep_count(particles, dt)
        candidates = self._particle_pair_candidates(particles)
        candidates.extend(self._surface_candidates(particles, dt))
        candidates.sort(key=self._candidate_sort_key)

        over_budget = (
            math.isfinite(self.budget_ms)
            and (self.budget_ms <= 0.0 or elapsed_ms >= self.budget_ms)
            and bool(candidates)
        )
        if over_budget:
            if elapsed_ms > 0.0:
                ratio = max(0.0, self.budget_ms / elapsed_ms)
            else:
                ratio = 0.0
            keep = max(1, int(math.ceil(len(candidates) * min(1.0, ratio))))
            selected = candidates[:keep]
            if keep < len(candidates):
                threshold = selected[-1]["priority"]
                if any(
                    candidate["priority"] < threshold
                    for candidate in candidates
                ):
                    # Do not split a priority tier across the budget line;
                    # all work for the highest-priority particles is kept.
                    selected = [
                        candidate for candidate in candidates
                        if candidate["priority"] >= threshold
                    ]
            selected_set = {id(candidate) for candidate in selected}
            skipped = [
                candidate for candidate in candidates
                if id(candidate) not in selected_set
            ]
        else:
            selected = candidates
            skipped = []

        processed_ids = []
        allowed_surface = set()
        for candidate in selected:
            processed_ids.extend(candidate["pids"])
            if candidate["kind"] == "surface":
                allowed_surface.add(
                    (candidate["pids"][0], candidate["triangle_id"]),
                )
        if not over_budget:
            allowed_surface = None

        skipped_ids = []
        for candidate in skipped:
            skipped_ids.extend(candidate["pids"])

        pairs = 0
        fallback_materials = 0
        dt_substep = dt / substeps
        for _ in range(substeps):
            for pid in sorted(particles, key=_pid_key):
                particle = particles[pid]
                contacts, fallbacks = self._resolve_particle(
                    particle, dt_substep, allowed_surface,
                )
                pairs += contacts
                fallback_materials += fallbacks

        with self._hash_lock:
            for pid, particle in particles.items():
                if pid in self._particles:
                    self._particles[pid] = particle
            self._rebuild_particle_hash_locked()

        ordered = sorted(particles.items(), key=lambda item: _pid_key(item[0]))
        return {
            "pairs": pairs,
            "substeps": substeps,
            "positions": {
                pid: particle.position for pid, particle in ordered
            },
            "velocities": {
                pid: particle.velocity for pid, particle in ordered
            },
            "degraded": bool(over_budget),
            "candidates": len(candidates),
            "processed": len(selected),
            "processed_ids": sorted(set(processed_ids), key=_pid_key),
            "skipped_ids": sorted(set(skipped_ids), key=_pid_key),
            "fallback_materials": fallback_materials,
            "elapsed_ms": elapsed_ms,
        }

    def stats(self):
        with self._hash_lock:
            hash_particles = sum(
                len(pids) for pids in self._particle_hash.values()
            )
            return {
                "triangles": len(self._triangles),
                "particles": len(self._particles),
                "hash_particles": hash_particles,
                "hash_cells": len(self._particle_hash),
                "cell_size": self.cell_size,
            }
