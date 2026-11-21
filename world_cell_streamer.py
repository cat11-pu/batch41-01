"""World cell streaming with priorities, refcounts and main-thread callbacks."""

from __future__ import annotations

import math
import time
from collections import defaultdict


class WorldCellStreamer:
    """Small deterministic model of an open-world cell streamer.

    The streamer deliberately keeps the IO side and the scene side separate.
    ``update`` only decides which cells should be requested and records them as
    pending; ``complete`` queues a main-thread event which invokes callbacks.
    """

    _REQUEST_COST_MS = 0.05

    def __init__(self, cell_size=64.0, load_radius=2, hysteresis=1):
        self.cell_size = float(cell_size)
        self.load_radius = max(0, int(load_radius))
        self.hysteresis = max(0, int(hysteresis))
        if self.cell_size <= 0.0:
            raise ValueError("cell_size must be positive")

        self.player = (0.0, 0.0)
        self.heading_deg = 0.0

        self.refs = {}
        self.resident = set()
        self.loaded = set()
        self.pending = {}
        self.empty = set()
        self.dependencies = {}
        self.callbacks = []
        self.main_queue = []

        self._cancelled_cells = set()
        self._empty_seen = set()
        self._empty_hits = 0
        self._unloads = 0
        self._completions = 0
        self._cancelled_total = 0
        self._cancelled_since_update = 0

    @staticmethod
    def _as_cell(cell):
        try:
            x, z = cell
        except (TypeError, ValueError):
            raise ValueError("cell must be a (x, z) pair") from None
        return (int(x), int(z))

    def cell_at(self, x, z):
        """Map world coordinates to integer cells, including negative values."""
        return (
            math.floor(float(x) / self.cell_size),
            math.floor(float(z) / self.cell_size),
        )

    def set_player(self, position, heading_deg=0.0):
        try:
            x, z = position
        except (TypeError, ValueError):
            raise ValueError("position must be an (x, z) pair") from None
        self.player = (float(x), float(z))
        self.heading_deg = float(heading_deg) % 360.0

    def request(self, cell, owner="system"):
        """Add one reference owner to a cell; duplicate owners are idempotent."""
        cell = self._as_cell(cell)
        owners = self.refs.setdefault(cell, set())
        if owner in owners:
            return False
        owners.add(owner)
        return True

    def release(self, cell, owner="system"):
        """Drop one owner. Actual unloading is deferred until ``update``."""
        cell = self._as_cell(cell)
        owners = self.refs.get(cell)
        if not owners or owner not in owners:
            return False
        owners.remove(owner)
        if not owners:
            self.refs.pop(cell, None)
        return True

    def add_dependency(self, cell, dependency):
        """Declare that ``cell`` is only complete with ``dependency`` loaded."""
        cell = self._as_cell(cell)
        dependency = self._as_cell(dependency)
        self.dependencies.setdefault(cell, set()).add(dependency)

    def mark_empty(self, cell):
        """Mark a cell as having no content, so it is never requested."""
        cell = self._as_cell(cell)
        self.empty.add(cell)
        self.pending.pop(cell, None)
        self.resident.discard(cell)
        self.loaded.discard(cell)
        self._cancelled_cells.discard(cell)

    def register_callback(self, callback):
        if not callable(callback):
            raise TypeError("callback must be callable")
        self.callbacks.append(callback)

    def complete(self, cell, thread="io"):
        """Queue an IO completion; user callbacks never run on the IO thread."""
        cell = self._as_cell(cell)
        if cell in self.empty:
            return False

        cancelled = cell in self._cancelled_cells and cell not in self.pending
        known = cell in self.pending or cell in self.resident or cell in self.refs
        if cancelled or not known:
            return False

        was_loaded = cell in self.loaded
        if was_loaded and cell not in self.pending:
            return False

        self.pending.pop(cell, None)
        self._cancelled_cells.discard(cell)
        self.resident.add(cell)
        self.loaded.add(cell)
        self._completions += 1
        self.main_queue.append((cell, thread))
        return True

    def pump_main_thread(self):
        """Invoke queued completion callbacks on the calling (main) thread."""
        count = 0
        while self.main_queue:
            cell, _thread = self.main_queue.pop(0)
            for callback in tuple(self.callbacks):
                callback(cell)
            count += 1
        return count

    def teleport(self, position):
        """Cancel requests for the old area immediately and move the player."""
        count = 0
        for cell in tuple(self.pending):
            if self.refs.get(cell):
                continue
            self.pending.pop(cell, None)
            self.resident.discard(cell)
            self.loaded.discard(cell)
            self._cancelled_cells.add(cell)
            count += 1

        self._cancelled_total += count
        self._cancelled_since_update += count
        self.set_player(position)
        return count

    def _dependency_closure(self, roots):
        result = set()
        stack = list(roots)
        while stack:
            cell = stack.pop()
            if cell in result:
                continue
            result.add(cell)
            stack.extend(self.dependencies.get(cell, ()))
        return result

    def _priority_key(self, cell, player_cell):
        dx = cell[0] - player_cell[0]
        dz = cell[1] - player_cell[1]

        # Use grid distance so all cells in the same ring have equal priority;
        # heading then breaks the tie in favour of the view cone.
        distance_sq = float(dx * dx + dz * dz)

        if dx == 0 and dz == 0:
            angle = 0.0
        else:
            # Heading 0 points towards +Z; +X is to the right.
            bearing = math.degrees(math.atan2(dx, dz)) % 360.0
            angle = abs((bearing - self.heading_deg + 180.0) % 360.0 - 180.0)

        return (distance_sq, angle, cell[0], cell[1])

    def _dependency_groups(self, cells):
        """Return connected components of required cells joined by dependencies."""
        adjacency = defaultdict(set)
        for cell in cells:
            for dependency in self.dependencies.get(cell, ()):
                if dependency in cells:
                    adjacency[cell].add(dependency)
                    adjacency[dependency].add(cell)

        groups = []
        seen = set()
        for start in sorted(cells):
            if start in seen:
                continue
            group = set()
            stack = [start]
            while stack:
                cell = stack.pop()
                if cell in group:
                    continue
                group.add(cell)
                seen.add(cell)
                stack.extend(adjacency.get(cell, ()))
            groups.append(group)
        return groups

    def update(self, budget_ms=2.0):
        """Plan one frame: unload, prioritise, then issue requests within budget."""
        start = time.perf_counter()
        try:
            budget = max(0.0, float(budget_ms))
        except (TypeError, ValueError):
            raise ValueError("budget_ms must be a number") from None

        player_cell = self.cell_at(*self.player)
        load_radius = self.load_radius
        view_cells = {
            (player_cell[0] + dx, player_cell[1] + dz)
            for dx in range(-load_radius, load_radius + 1)
            for dz in range(-load_radius, load_radius + 1)
        }

        active_before = set(self.resident) | set(self.pending)
        retention_radius = load_radius + self.hysteresis
        retained = {
            cell
            for cell in active_before
            if max(
                abs(cell[0] - player_cell[0]),
                abs(cell[1] - player_cell[1]),
            )
            <= retention_radius
        }

        roots = set(view_cells) | set(self.refs) | retained
        required = self._dependency_closure(roots)
        required_nonempty = {cell for cell in required if cell not in self.empty}

        unloads = 0
        for cell in sorted(active_before - required):
            if self.refs.get(cell):
                continue
            self.pending.pop(cell, None)
            self.resident.discard(cell)
            self.loaded.discard(cell)
            unloads += 1
        self._unloads += unloads

        required_empty = required & self.empty
        for cell in required_empty - self._empty_seen:
            self._empty_hits += 1
        self._empty_seen = required_empty

        new_cells = {
            cell
            for cell in required_nonempty
            if cell not in self.resident and cell not in self.pending
        }
        priority_order = sorted(
            new_cells, key=lambda cell: self._priority_key(cell, player_cell)
        )

        groups = self._dependency_groups(required_nonempty)
        cell_to_group = {}
        for index, group in enumerate(groups):
            for cell in group:
                cell_to_group[cell] = index

        protected_cells = {
            player_cell,
            (player_cell[0] + 1, player_cell[1]),
            (player_cell[0] - 1, player_cell[1]),
            (player_cell[0], player_cell[1] + 1),
            (player_cell[0], player_cell[1] - 1),
        }
        protected_groups = {
            cell_to_group[cell] for cell in protected_cells if cell in cell_to_group
        }

        def group_order(index):
            return min(
                (
                    self._priority_key(cell, player_cell)
                    for cell in groups[index]
                    if cell in new_cells
                ),
                default=(math.inf, math.inf, 0, 0),
            )

        selected = set()
        spent_ms = 0.0

        # A structure spanning cells is requested as one unit. Cells under and
        # immediately around the player are never deferred by the time budget.
        for index in sorted(protected_groups, key=group_order):
            members = groups[index] & new_cells
            selected.update(members)
            spent_ms += len(members) * self._REQUEST_COST_MS

        normal_groups = [
            index for index in range(len(groups)) if index not in protected_groups
        ]
        normal_groups.sort(key=group_order)
        for index in normal_groups:
            members = groups[index] & new_cells
            if not members:
                continue
            cost = len(members) * self._REQUEST_COST_MS
            if spent_ms + cost > budget + 1e-9:
                break
            selected.update(members)
            spent_ms += cost

        requests = [cell for cell in priority_order if cell in selected]
        for cell in requests:
            self.pending[cell] = {"thread": "io", "budget_ms": budget}
            self.resident.add(cell)
            self.loaded.discard(cell)
            self._cancelled_cells.discard(cell)

        cancelled = self._cancelled_since_update
        self._cancelled_since_update = 0
        elapsed_ms = max(0.0, spent_ms)
        if elapsed_ms == 0.0:
            elapsed_ms = max(0.0, (time.perf_counter() - start) * 1000.0)

        return {
            "requests": requests,
            "deferred": len(new_cells) - len(requests),
            "loading": len(self.pending),
            "resident": len(self.resident),
            "resident_cells": sorted(self.resident),
            "unloads": unloads,
            "cancelled": cancelled,
            "empty_hits": self._empty_hits,
            "elapsed_ms": elapsed_ms,
            "budget_ms": budget,
        }

    def stats(self):
        return {
            "loading": len(self.pending),
            "resident": len(self.resident),
            "resident_cells": sorted(self.resident),
            "pending_cells": sorted(self.pending),
            "ref_counts": {
                cell: len(owners) for cell, owners in self.refs.items() if owners
            },
            "unloads": self._unloads,
            "cancelled": self._cancelled_total,
            "empty_hits": self._empty_hits,
            "completed": self._completions,
            "main_queue": len(self.main_queue),
        }
