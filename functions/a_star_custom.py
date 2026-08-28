"""
a_star_custom.py — grid A* for the aggregate twin.

This module does one thing: search. All geometry (origin, resolution, shape,
world <-> cell conversion) and all cost (distance, uncertainty, risk) come from
:class:`GlobalGridMap`; agent physics (energy, time) is not represented here at
all — it is bid by the instance twin, which owns the battery model.

The only agent property the search uses is the footprint radius, which is
checked against the map's distance field rather than baked into the occupancy
grid, so one map serves agents of any size.

author: Atsushi Sakai (@Atsushi_twi)
        Nikos Kanargias (nkana@tee.gr)
adapted by: Reinis Cimurs
further customized for project specific use by: Kobe Frateur

See https://en.wikipedia.org/wiki/A*_search_algorithm
"""

from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from functions.grid_map import GlobalGridMap

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Result
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PlanResult:
    """Outcome of one search.

    path          : (2, N) world-metre waypoints, ordered start → goal.
    cost          : posture-weighted cost accumulated by the search.
    distance      : [m] polyline length — this is what the bid is computed from.
    min_clearance : [m] tightest squeeze along the path.
    expanded      : nodes closed; the number to watch when tuning.
    reason        : why an infeasible plan failed.
    """

    path: np.ndarray
    cost: float
    distance: float = 0.0
    min_clearance: float = 0.0
    expanded: int = 0
    reason: str | None = None

    @property
    def feasible(self) -> bool:
        return self.path.shape[1] >= 2 and math.isfinite(self.cost)

    @classmethod
    def failed(cls, reason: str, expanded: int = 0) -> "PlanResult":
        return cls(path=np.empty((2, 0)), cost=math.inf, reason=reason, expanded=expanded)


def _as_xy(pose: Any) -> tuple[float, float]:
    """Accepts (2,1)/(3,1) column vectors, flat arrays, tuples or lists."""
    arr = np.asarray(pose, dtype=float).reshape(-1)
    if arr.size < 2:
        raise ValueError(f"pose needs at least x and y, got {pose!r}")
    return float(arr[0]), float(arr[1])


# ══════════════════════════════════════════════════════════════════════════
# Planner
# ══════════════════════════════════════════════════════════════════════════

class AStarPlannerCustom:
    """8-connected A* over a :class:`GlobalGridMap`."""

    # dx, dy, step multiplier
    MOTION: tuple[tuple[int, int, float], ...] = (
        (1, 0, 1.0),
        (0, 1, 1.0),
        (-1, 0, 1.0),
        (0, -1, 1.0),
        (-1, -1, math.sqrt(2)),
        (-1, 1, math.sqrt(2)),
        (1, -1, math.sqrt(2)),
        (1, 1, math.sqrt(2)),
    )

    def __init__(
        self,
        grid_map: GlobalGridMap,
        epsilon: float = 1.0,
        allow_corner_cutting: bool = False,
    ) -> None:
        """
        Args:
            grid_map : the map to plan on; also the source of all geometry.
            epsilon  : heuristic inflation. 1.0 is optimal; >1 is bounded
                       suboptimal (cost <= eps * optimal) and expands far fewer
                       nodes. Note that with eps > 1 the returned costs are no
                       longer directly comparable between agents, which matters
                       if you rank candidates on them.
            allow_corner_cutting : permit diagonal moves that squeeze between
                       two blocked cells.
        """
        if epsilon < 1.0:
            raise ValueError("epsilon must be >= 1.0 to stay bounded-suboptimal")
        self.gm = grid_map
        self.epsilon = float(epsilon)
        self.allow_corner_cutting = bool(allow_corner_cutting)

    # ── public API ────────────────────────────────────────────────────────

    def planning(
        self,
        start_pose: Any,
        goal_pose: Any,
        weights: Sequence[float],
        agent_radius: float = 0.0,
        smooth: bool = False,
    ) -> PlanResult:
        """Search for a path from start_pose to goal_pose.

        Args:
            start_pose, goal_pose : world-metre (x, y), any array-like.
            weights      : (Wd, We, Wt, Wu, Wr); only Wd, Wu, Wr are used here.
            agent_radius : footprint radius [m]; cells with less clearance are
                           not traversable.
            smooth       : shortcut the staircase path. `cost` then remains the
                           pre-smoothing search cost; distance and clearance are
                           recomputed on the smoothed polyline.
        """
        gm = self.gm

        # Bind the derived layers once: GlobalGridMap rebinds on invalidation
        # rather than mutating, so these stay a consistent snapshot even if
        # perception updates the map on another thread mid-search.
        occupied = gm.occupied
        distance_field = gm.distance_field
        per_metre, per_cell = gm.cost_layers(weights)
        c_min = gm.min_cost_per_meter(weights) * self.epsilon
        resolution = gm.res
        ny = gm.ny

        start = gm.world_to_cell(*_as_xy(start_pose))
        goal = gm.world_to_cell(*_as_xy(goal_pose))

        reason = self._validate(start, goal, occupied, distance_field, agent_radius)
        if reason is not None:
            logger.debug("[A*] infeasible before search: %s", reason)
            return PlanResult.failed(reason)

        start_id = start[0] * ny + start[1]
        goal_id = goal[0] * ny + goal[1]

        g_cost: dict[int, float] = {start_id: 0.0}
        parent: dict[int, int] = {start_id: -1}
        closed: set[int] = set()

        h0 = self._heuristic(start, goal, resolution, c_min)
        open_heap: list[tuple[float, float, int]] = [(h0, h0, start_id)]

        found = False
        while open_heap:
            _f, _h, current_id = heapq.heappop(open_heap)
            if current_id in closed:
                continue
            closed.add(current_id)

            if current_id == goal_id:
                found = True
                break

            cx, cy = divmod(current_id, ny)
            current_g = g_cost[current_id]

            for dx, dy, step in self.MOTION:
                gx, gy = cx + dx, cy + dy

                if not gm.in_bounds(gx, gy):
                    continue
                if occupied[gx, gy]:
                    continue
                if distance_field[gx, gy] < agent_radius:
                    continue  # robot does not fit
                if dx and dy and not self.allow_corner_cutting:
                    if occupied[cx + dx, cy] or occupied[cx, cy + dy]:
                        continue

                neighbour_id = gx * ny + gy
                if neighbour_id in closed:
                    continue

                step_dist = step * resolution
                tentative = current_g + float(
                    per_metre[gx, gy] * step_dist + per_cell[gx, gy]
                )
                if tentative >= g_cost.get(neighbour_id, math.inf):
                    continue

                g_cost[neighbour_id] = tentative
                parent[neighbour_id] = current_id
                h = self._heuristic((gx, gy), goal, resolution, c_min)
                # h is the tie-break key: on equal f, lean toward the goal.
                heapq.heappush(open_heap, (tentative + h, h, neighbour_id))

        if not found:
            logger.debug("[A*] open set exhausted after %d expansions", len(closed))
            return PlanResult.failed("unreachable", expanded=len(closed))

        cells = self._trace(goal_id, parent, ny)
        if smooth:
            cells = self._shortcut(cells, distance_field, agent_radius)

        path = self._to_world(cells)
        return PlanResult(
            path=path,
            cost=float(g_cost[goal_id]),
            distance=self.path_length(path),
            min_clearance=float(min(distance_field[cx, cy] for cx, cy in cells)),
            expanded=len(closed),
        )

    @staticmethod
    def path_length(path: np.ndarray) -> float:
        """Polyline length [m] of a (2, N) path."""
        if path.ndim != 2 or path.shape[1] < 2:
            return 0.0
        return float(np.hypot(np.diff(path[0]), np.diff(path[1])).sum())

    # ── internals ─────────────────────────────────────────────────────────

    def _validate(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        occupied: np.ndarray,
        distance_field: np.ndarray,
        agent_radius: float,
    ) -> str | None:
        """Reject hopeless queries before expanding anything.

        Without the region test an unreachable goal costs a full-map expansion,
        which matters when candidate selection runs K searches per mission.
        """
        gm = self.gm
        for name, cell in (("start", start), ("goal", goal)):
            if not gm.in_bounds(*cell):
                return f"{name}_out_of_bounds"
            if occupied[cell]:
                return f"{name}_occupied"
            if distance_field[cell] < agent_radius:
                return f"{name}_too_tight"
        if not gm.reachable(start, goal):
            return "disconnected"
        return None

    @staticmethod
    def _heuristic(
        cell: tuple[int, int],
        goal: tuple[int, int],
        resolution: float,
        c_min_per_metre: float,
    ) -> float:
        """Straight-line distance times the cheapest possible cost per metre.

        Admissible as long as `c_min_per_metre` really is the floor of what
        GlobalGridMap.cell_cost can charge — which is why that bound lives in
        the map, next to the cost function it has to track.
        """
        d = math.hypot(cell[0] - goal[0], cell[1] - goal[1]) * resolution
        return d * c_min_per_metre

    @staticmethod
    def _trace(goal_id: int, parent: dict[int, int], ny: int) -> list[tuple[int, int]]:
        """Walk parents back from the goal and return cells start → goal."""
        cells: list[tuple[int, int]] = []
        node = goal_id
        while node != -1:
            cells.append(divmod(node, ny))
            node = parent[node]
        cells.reverse()
        return cells

    def _to_world(self, cells: Sequence[tuple[int, int]]) -> np.ndarray:
        pts = [self.gm.cell_to_world(cx, cy) for cx, cy in cells]
        return np.array(pts, dtype=float).T if pts else np.empty((2, 0))

    def _shortcut(
        self,
        cells: list[tuple[int, int]],
        distance_field: np.ndarray,
        agent_radius: float,
    ) -> list[tuple[int, int]]:
        """String-pull the 8-connected staircase into straight segments."""
        if len(cells) < 3:
            return cells
        out = [cells[0]]
        i = 0
        while i < len(cells) - 1:
            j = len(cells) - 1
            while j > i + 1 and not self._line_clear(cells[i], cells[j],
                                                     distance_field, agent_radius):
                j -= 1
            out.append(cells[j])
            i = j
        return out

    @staticmethod
    def _line_clear(
        a: tuple[int, int],
        b: tuple[int, int],
        distance_field: np.ndarray,
        agent_radius: float,
    ) -> bool:
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1])) + 1
        xs = np.rint(np.linspace(a[0], b[0], n)).astype(int)
        ys = np.rint(np.linspace(a[1], b[1], n)).astype(int)
        return bool((distance_field[xs, ys] >= agent_radius).all())