"""
mission_planner.py  —  Mission Definition and Assignment Framework
"""

from __future__ import annotations

import logging

import numpy as np

from core.fleet.agent_entry import AgentEntry
from core_msgs.instance_aggregate.mission_handshake import MissionPlanHint
from core.functions.a_star_custom import PlanResult
from utils.mission_logger import MissionLogger
from core_msgs.instance_aggregate.mission import (
    Mission,
    MissionStatus,
    MissionType,
    POSTURE_WEIGHTS, MissionPosture,
)

logger = logging.getLogger(__name__)


class MissionPlanner:
    """
    Handles mission assignment and path planning for the Overarching Twin.
    """

    def __init__(
            self,
            astar_planner_custom,
            grid_map,
            sim_time_fn,
            uav_world_map_fn,
            mission_logger: MissionLogger,
            safety_reserve: float = 10.0,
    ) -> None:
        self._planner_custom = astar_planner_custom
        self._grid_map = grid_map
        self._sim_time = sim_time_fn  # callable: () → float
        self._uav_world_map = uav_world_map_fn  # callable: () → dict
        self._safety_reserve = safety_reserve

        self.mission_logger = mission_logger

    def assign_and_plan(self, missions, ugv_list, k: int = 3):
        free = [u for u in ugv_list if u.mission_id is None]     # telemetry knows
        out = []
        for m in (x for x in missions if x.mission_status is MissionStatus.PENDING):
            goal = self._resolve_goal(m)
            if goal is None:
                continue
            weights = POSTURE_WEIGHTS[m.mission_posture]
            scored = []
            for u in free:
                res = self._plan(u, u.xy, goal, weights)          # PlanResult
                if not res.feasible:
                    continue
                scored.append((res.cost, u.name , res))
            scored.sort(key=lambda t: t[0])
            if scored:
                out.append((m, {
                    name: MissionPlanHint(distance=r.distance, plan_cost=c,
                                           path=self._path_as_points(r.path))
                    for c, name, r in scored[:k]
                }))
        return out


    def get_winner(self, mission, bids, hints : dict[str, MissionPlanHint]) -> str | None:
        Wd, We, Wt, _Wu, _Wr = POSTURE_WEIGHTS[mission.mission_posture]
        floor = mission.battery_threshold if mission.battery_threshold is not None \
            else self._safety_reserve
        ok = {n: b for n, b in bids.items()
              if b and b.time_bidding is not None and b.battery_margin is not None
              and b.battery_margin > floor
              and (mission.battery_budget is None or b.battery_bidding <= mission.battery_budget)}
        if not ok:
            return None
        t = _norm([b.time_bidding for b in ok.values()])
        e = _norm([b.battery_bidding for b in ok.values()])
        return min(ok, key=lambda n: Wt * t(ok[n].time_bidding) + We * e(ok[n].battery_bidding) + hints[n].plan_cost )



    # TODO : Not used
    def replan(
            self,
            ugv : AgentEntry,
            mission: Mission,
            weights: tuple,
            reason: str = "triggered",
    ) -> list[tuple[float, float]] | None:
        """Replan a specific UGV/mission pair."""
        goal = self._resolve_goal(mission)
        if goal is None:
            logger.debug("replan skipped: mission=%s has no resolvable goal (reason=%s)",
                               mission.mission_id, reason)
            return None

        result = self._plan(ugv, ugv.xy, goal, weights)
        if not result.feasible:
            logger.warning(
                "replan infeasible: agent=%s mission=%s reason=%s (%s)",
                ugv.name, mission.mission_id, reason, result.reason,
            )
            return None

        logger.info(
            "replanned agent=%s mission=%s reason=%s cost=%.2f distance=%.2f",
            ugv.name, mission.mission_id, reason, result.cost, result.distance,
        )
        return self._path_as_points(result.path)

    # TODO : Not used
    def posture_for_battery(self, battery_pct: float) -> MissionPlanHint:
        """
        Return the recommended PBPA posture based on battery state.
        """
        if battery_pct > 60:
            return MissionPosture.URGENT
        if battery_pct > 30:
            return MissionPosture.CONSERVE
        return MissionPosture.URGENT

    @property
    def assignment_log(self) -> list[dict]:
        return self._assignment_log

    # ── Private helpers ───────────────────────────────────────────────────────

    def _plan(self, ugv, start_xy, goal_xy, weights) -> PlanResult:
        return self._planner_custom.planning(
            start_pose=start_xy,
            goal_pose=goal_xy,
            weights=weights,
            agent_radius=getattr(ugv, "radius", 0.25),
        )

    @staticmethod
    def _path_as_points(path: np.ndarray) -> list[tuple[float, float]]:
        return [(float(x), float(y)) for x, y in zip(path[0], path[1])]

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


def _norm(values):
    lo, hi = min(values), max(values)
    return (lambda v: 0.0) if hi - lo < 1e-12 else (lambda v: (v - lo) / (hi - lo))