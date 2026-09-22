"""World sheet: every agent, every observation, every mission."""
from __future__ import annotations

from typing import Any

from core_msgs.instance_aggregate.mission import MissionPosture, MissionStatus, MissionType

from aggregate_twin.gui.discovery_view import review_state
from aggregate_twin.gui.view_helpers import (
    enum_name, header, obstacle, route_points, shape_of, velocity, xy,
)


def world_state(twin: Any, monitor: Any) -> dict[str, Any]:
    wc = twin.world_config
    return {
        "twin": header(twin, monitor),
        "review": review_state(twin),
        "world": {"width": wc.width, "height": wc.height,
                  "origin_x": wc.origin_x, "origin_y": wc.origin_y,
                  "resolution": wc.resolution},
        "agents": _agents(twin),
        "obstacles": _obstacles(twin),
        "missions": _missions(twin),
        "options": {"types": MissionType.get_names(), "postures": MissionPosture.get_names()},
    }


def _agents(twin: Any) -> list[dict[str, Any]]:
    stale_after = getattr(twin.fleet, "stale_after", 3.0)
    routes = {m.assigned_ugv: m.path for m in twin.active_missions if m.assigned_ugv}
    agents = []
    for entry in twin.fleet.all(include_stale=True):
        x, y, theta = xy(entry.state)
        v, w = velocity(entry.velocity)
        kind = enum_name(entry.kind, "ugv")
        agents.append({
            "name": entry.name,
            "instance": entry.instance_name,
            "kind": kind,
            "x": x, "y": y, "theta": theta,
            "v": round(v, 3), "w": round(w, 3),
            "battery": entry.battery_pct,
            "arrived": bool(entry.arrive_flag),
            "mission_id": entry.mission_id,
            "age": round(entry.age, 2),
            "stale": entry.age > stale_after,
            "shape": shape_of(twin.fleet.discovery_of(entry.name), kind),
            "path": route_points(routes.get(entry.name)),
        })
    return agents


def _obstacles(twin: Any) -> list[dict[str, Any]]:
    found = [obstacle(o, "world") for o in getattr(twin.world_config, "static_obstacles", None) or []]
    for report in twin.obstacles.all_reports():
        if report.obs is not None and report.reporter is not None:
            found.append(obstacle(report.obs, report.reporter))
    return found


def _missions(twin: Any) -> list[dict[str, Any]]:
    missions = []
    for m in twin.missions:
        missions.append({
            "id": m.mission_id,
            "type": enum_name(m.mission_type),
            "posture": enum_name(m.mission_posture),
            "status": enum_name(m.mission_status),
            "goal": list(m.goal_xy) if m.goal_xy else None,
            "waypoints": [list(w) for w in (m.waypoints or [])],
            "target_id": m.target_id,
            "unlock_time": m.unlock_time,
            "assigned": m.assigned_ugv,
            "cost": None if m.last_cost in (None, float("inf")) else round(float(m.last_cost), 2),
            "distance": None if m.distance is None else round(float(m.distance), 2),
            "path": route_points(m.path),
            "in_flight": m.mission_status is MissionStatus.BIDDING,
        })
    return missions
