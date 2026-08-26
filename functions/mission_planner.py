"""
mission_planner.py  —  Mission Definition and Assignment Framework
==================================================================
Implements the mission layer of the HDT architecture.

=== Changes made in this pass (search "FIX:") ===
1. Import of POSTURE_WEIGHTS/Mission/MissionType previously came from
   `aggregate_twin` -- which itself imports THIS module, i.e. a circular
   import. aggregate_twin.py also never defines POSTURE_WEIGHTS and never
   imports MissionType, so this raised ImportError before anything ran.
   Now imported from core_msgs.instance_aggregate.mission directly (same
   module aggregate_twin.py already pulls Mission/MissionStatus from).
   ASSUMPTION: POSTURE_WEIGHTS and MissionType actually live in that module
   -- adjust the import path below if they live somewhere else (e.g. a
   dedicated constants module).
2. This file used `mission.status` (plain string literals "pending"/
   "active"/"failed"), while aggregate_twin.py uses `mission.mission_status`
   (the MissionStatus enum) for the exact same concept. Two different
   attributes that were never kept in sync -- aggregate_twin's
   `_apply_new_paths` would never see this module's assignment, and failed
   missions would never surface upstream. Unified on `mission_status` +
   MissionStatus enum, matching aggregate_twin.py.
3. `_check_battery`'s return type hint said `-> None` but the function
   returns True/False and its docstring claimed it *raises*
   BatteryConstraintError (it doesn't) -- fixed the hint/docstring to match
   actual behavior.
4. The "original AStarPlanner" branch in `_plan()` called
   `self._planner.planning(start_pose=..., goal_pose=..., show_animation=False)`
   with none of the required `weights`/`global_grid_map`/`ugv` args that
   AStarPlannerCustom.planning() needs -- and in aggregate_twin.py,
   `astar_planner` and `astar_planner_custom` are literally the same
   AStarPlannerCustom instance, so there's no separate "original" planner
   to call anyway. Left the branch in place but guarded + logged so it
   degrades to the custom planner instead of crashing, since there's no
   distinct planner class provided to actually wire in here.
5. Debug logging added around assignment/replanning so mission flow is
   observable.
"""

from __future__ import annotations

import logging
import math
import time

import numpy as np
from loggers.mission_logger import MissionLogger
from core_msgs.instance_aggregate.mission import (
    Mission,
    MissionStatus,
    MissionType,
    POSTURE_WEIGHTS,
)

logger = logging.getLogger(__name__)


class MissionPlanner:
    """
    Handles mission assignment and path planning for the Overarching Twin.
    """

    def __init__(
            self,
            astar_planner,
            astar_planner_custom,
            grid_map,
            sim_time_fn,
            uav_world_map_fn,
            mission_logger: MissionLogger,
            safety_reserve: float = 10.0,
    ) -> None:
        self._planner = astar_planner
        self._planner_custom = astar_planner_custom
        self._grid_map = grid_map
        self._sim_time = sim_time_fn  # callable: () → float
        self._uav_world_map = uav_world_map_fn  # callable: () → dict
        self._safety_reserve = safety_reserve

        self.mission_logger = mission_logger

    def assign_and_plan(
            self,
            missions: list[Mission],
            ugv_list: list,
    ) -> dict[str, np.ndarray]:
        """
        Assign pending missions to available UGVs and plan paths.
        """
        pending = [m for m in missions if m.mission_status == MissionStatus.PENDING]
        available = [u for u in ugv_list
                     if not self._ugv_busy(u, missions)]

        logger.debug(
            "[MissionPlanner] assign_and_plan: %d pending mission(s), %d available ugv(s)",
            len(pending), len(available),
        )

        if not pending or not available:
            return {}

        # Cost matrix
        n_ugv = len(available)
        n_mis = len(pending)
        C = np.full((n_ugv, n_mis), fill_value=1e9)

        # Dictionary to store the correct paths
        saved_paths = {}

        # For each mission pending
        for j, mission in enumerate(pending):
            goal = self._resolve_goal(mission)

            # Then we go over all available ugvs and check cost path
            for i, ugv in enumerate(available):
                if goal is None:
                    continue  # goal not yet available

                start_xy = ugv.state
                t0 = time.perf_counter()
                path, cost = self._plan(ugv, start_xy, goal, POSTURE_WEIGHTS[mission.mission_posture])

                path_lenght = len(path[0])
                if path_lenght < 2:
                    logger.debug(
                        "[MissionPlanner] robot=%s cannot complete mission=%s: path infeasible",
                        self._ugv_id(ugv), mission.mission_id,
                    )
                    C[i, j] = 1e9  # infeasible
                    continue

                logger.debug(
                    "[MissionPlanner] path planned length=%d %.0fms",
                    len(path[0]), (time.perf_counter() - t0) * 1000,
                )

                if self._check_battery(ugv, path):
                    C[i, j] = cost
                    mission.last_cost = cost
                    # NEW: Save the path mapped to this specific UGV (i) and Mission (j)
                    saved_paths[(i, j)] = path
                else:
                    logger.debug(
                        "[MissionPlanner] robot=%s cannot complete mission=%s: battery too low",
                        self._ugv_id(ugv), mission.mission_id,
                    )
                    C[i, j] = 1e9  # infeasible

                self.mission_logger.update_per_mission_log(mission.mission_id, self._ugv_id(ugv), path, cost, C[i, j])

        # Hungarian assignment based on cost matrix
        try:
            from scipy.optimize import linear_sum_assignment
            row_idx, col_idx = linear_sum_assignment(C)
        except ImportError:
            logger.warning("[MissionPlanner] scipy not available, falling back to greedy assignment")
            # Fallback: greedy nearest assignment
            row_idx, col_idx = self._greedy_assign(C)

        result: dict[str, np.ndarray] = {}

        # Finalize assignment
        for i, j in zip(row_idx, col_idx, strict=False):
            if C[i, j] >= 1e8:
                continue  # no feasible assignment

            ugv = available[i]
            mission = pending[j]
            ugv_id = self._ugv_id(ugv)

            # Re-extract the specific goal for the print log
            actual_goal = self._resolve_goal(mission)

            mission.assigned_ugv = ugv_id
            # FIX: was `mission.status = "active"` -- see module docstring.
            mission.mission_status = MissionStatus.ACTIVE
            ugv.assigned_mission = mission

            # Retrieve the correct path and cost
            assigned_path = saved_paths[(i, j)]
            assigned_cost = C[i, j]

            result[ugv_id] = assigned_path

            self.mission_logger.update_assignment_log(
                mission.mission_id,
                ugv_id,
                self._sim_time(),
                assigned_cost,
                "initial_assignment")

            logger.debug(
                "[MissionPlanner] linked mission=%s -> ugv=%s cost=%.2f goal=%s",
                mission.mission_id, ugv_id, assigned_cost, actual_goal,
            )

        # Fail unfeasible missions
        for mission in pending:
            if mission.mission_status == MissionStatus.PENDING:
                logger.debug("[MissionPlanner] mission=%s could not be assigned this round", mission.mission_id)
                # FIX: was `mission.status = "failed"`.
                # ASSUMPTION: MissionStatus has a FAILED member -- adjust if
                # your enum names this differently (e.g. UNASSIGNED).
                mission.mission_status = MissionStatus.FAILED

        return result

    def replan(
            self,
            ugv,
            mission: Mission,
            weights: tuple,
            reason: str = "triggered",
    ) -> np.ndarray | None:
        """
        Replan a specific UGV/mission pair.  Called by the Overarching Twin
        when a dynamic obstacle enters the path or battery drops.
        """
        ugv_pos = self._ugv_xy(ugv)
        goal = self._resolve_goal(mission)
        if goal is None:
            return None

        path, cost = self._plan(ugv, ugv_pos, goal, weights)
        if self._check_battery(ugv, path):
            self.mission_logger.update_assignment_log(
                mission.mission_id,
                self._ugv_id(ugv),
                self._sim_time(),
                cost,
                reason=reason)
            return path

        logger.debug("[MissionPlanner] replan failed for ugv=%s: battery too low", self._ugv_id(ugv))
        return None

    def posture_for_battery(self, battery_pct: float) -> str:
        """
        Return the recommended PBPA posture based on battery state.
        """
        if battery_pct > 60:
            return "EXPLORE"
        if battery_pct > 30:
            return "CONSERVE"
        return "URGENT"

    @property
    def assignment_log(self) -> list[dict]:
        return self._assignment_log

    # ── Private helpers ───────────────────────────────────────────────────────

    def _plan(
            self,
            ugv,
            start_xy: tuple,
            goal_xy: tuple,
            weights: tuple,
    ) -> tuple[np.ndarray, float]:
        """Run A* and return (path_array, total_cost)."""
        start = np.array([[start_xy[0]], [start_xy[1]]])
        goal = np.array([[goal_xy[0]], [goal_xy[1]]])

        if weights == POSTURE_WEIGHTS.get("ASTAR"):
            logger.warning(
                "[MissionPlanner] ASTAR posture requested but no distinct "
                "'original' planner is wired in -- using the custom planner instead."
            )

        path, cost = self._planner_custom.planning(
            start_pose=start,
            goal_pose=goal,
            weights=weights,
            global_grid_map=self._grid_map,
            ugv=ugv,
            show_animation=False,
        )
        return path, cost

    def _check_battery(self, ugv, path: np.ndarray) -> bool:
        """
        Returns True if the given path is within the UGV's battery budget,
        False otherwise.

        FIX: signature previously said `-> None` and the docstring claimed
        this raises BatteryConstraintError -- it does neither; it just
        returns a bool. Docstring/hint corrected to match actual behavior.
        """
        battery = getattr(ugv, 'battery_status', 100.0)
        mass = getattr(ugv, 'mass', 1.0)
        robot_avg_speed = getattr(ugv, 'avg_speed', 1.0)
        robot_anc_drain = getattr(ugv, 'ancillary_drain', 0)
        robot_friction = getattr(ugv, 'friction', 0.2)
        robot_joule_per = getattr(ugv, 'joule_per_percent', 700)
        robot_battery_scale = getattr(ugv, 'battery_scale', 30)

        budget = battery - self._safety_reserve

        energy_joule = predict_path_energy_joule(path, robot_mass=mass,
                                                 v_avg=robot_avg_speed,
                                                 Ka=robot_anc_drain,
                                                 Ku=robot_friction, )
        energy_per = (robot_battery_scale * energy_joule) / robot_joule_per

        logger.debug(
            "[MissionPlanner] battery check ugv=%s battery=%.1f safety=%.1f predicted=%.1f",
            self._ugv_id(ugv), battery, self._safety_reserve, energy_per,
        )

        if energy_per > budget:
            logger.debug(
                "[MissionPlanner] path needs %.1f%% battery, only %.1f%% available",
                energy_per, budget,
            )
            return False
        return True

    def _resolve_goal(
            self,
            mission: Mission,
    ) -> tuple[float, float] | None:
        """Extract the current (x, y) goal from any mission type."""
        if mission.mission_type == MissionType.GOTO_WAYPOINT:
            return mission.goal_xy

        if mission.mission_type == MissionType.TIME_GATED_GOTO:
            if self._sim_time() >= mission.unlock_time:
                return mission.goal_xy
            return None  # not yet unlocked

        if mission.mission_type == MissionType.TRACK_TARGET:
            world_map = self._uav_world_map()
            entry = world_map.get(mission.target_id)
            if entry:
                return (entry["estimated_pos"][0], entry["estimated_pos"][1])
            return None  # target not in UAV coverage

        return None

    def _ugv_id(self, ugv) -> str:
        return getattr(ugv, "id", None) or getattr(ugv, "name", str(id(ugv)))

    def _ugv_xy(self, ugv) -> tuple[float, float]:
        s = ugv.state
        if hasattr(s, "x"):
            return float(s.x), float(s.y)
        return float(s[0, 0]), float(s[1, 0])


    def _ugv_busy(self, ugv, missions: list[Mission]) -> bool:
        """True if this UGV already has an active mission."""
        ugv_id = self._ugv_id(ugv)
        return any(
            m.assigned_ugv == ugv_id and m.mission_status == MissionStatus.ACTIVE
            for m in missions
        )

    def _greedy_assign(self, C: np.ndarray) -> tuple:
        """
        Fallback greedy assignment when scipy is unavailable.
        Each UGV takes the cheapest available mission.
        """
        n_ugv, _n_mis = C.shape
        assigned_mis: set[int] = set()
        rows, cols = [], []
        for i in range(n_ugv):
            best_j = int(np.argmin(C[i]))
            if C[i, best_j] < 1e8 and best_j not in assigned_mis:
                rows.append(i)
                cols.append(best_j)
                assigned_mis.add(best_j)
        return rows, cols


def predict_path_energy_joule(
        path_xy: np.ndarray,
        robot_mass: float,
        v_avg: float = 0.4,
        Ka: float = 0.5,
        Ku: float = 0.05,
) -> float:
    """
    Estimate the battery percentage consumed to traverse path_xy.

    Parameters
    ----------
    path_xy     : (2, N) array of world-metre waypoints.
    robot_mass  : total mass including payload [kg].
    v_avg       : average speed [m/s].
    Ka          : ancillary power constant [W].
    Ku          : rolling friction coefficient.

    Returns
    -------
    float : estimated energy in normalised battery-% units.
            Calibrate the scale factor to your specific robot.
    """

    g = 9.81
    xs, ys = path_xy[0], path_xy[1]
    total = 0.0
    for i in range(len(xs) - 1):
        d = math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i])
        dt = d / max(v_avg, 1e-6)
        # Maneuvering energy [J]
        E_move = 2.0 * Ku * robot_mass * g * d
        # Ancillary energy [J]
        E_aux = Ka * dt
        total += E_move + E_aux

    return total
