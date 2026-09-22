"""
Every write the console makes.

Requests are checked here, on the HTTP thread, so the operator gets a precise answer
straight away. The change itself is handed to the twin's step thread through
`submit_command`, so nothing the console does races step().
"""
from __future__ import annotations

import logging
import time
from functools import partial
from typing import Any, Callable

from core_msgs.agents_contract import AgentKind
from core_msgs.global_msgs.global_payloads import AgentDiscoveryMessage
from core_msgs.instance_aggregate.mission import Mission, MissionPosture, MissionType

from aggregate_twin.core.comm_managers.link_manager import LinkingMode
from aggregate_twin.core.discovery_config import merge_layers
from aggregate_twin.gui.comm_monitor import CommMonitor
from aggregate_twin.gui.discovery_resolver import apply_resolution
from aggregate_twin.gui.discovery_validation import DiscoveryValidationError
from aggregate_twin.gui.discovery_view import pending_layers
from aggregate_twin.gui.http_router import ApiError, Conflict, NotFound

logger = logging.getLogger(__name__)


class ConsoleGateway:

    def __init__(self, twin: Any, monitor: CommMonitor) -> None:
        self.twin = twin
        self.monitor = monitor
        self.last_error: str | None = None
        self._warned_direct = False

    def _run(self, command: Callable[[], Any]) -> None:
        submit = getattr(self.twin, "submit_command", None)
        if callable(submit):
            submit(command)
            return
        if not self._warned_direct:
            self._warned_direct = True
            logger.warning("[console] twin has no submit_command(); console writes run "
                           "on the HTTP thread and can race step().")
        command()

    # -- missions ------------------------------------------------------------
    def dispatch_mission(self, spec: dict) -> str:
        mission = mission_from_spec(spec, {m.mission_id for m in self.twin.missions})
        self._run(partial(self.twin.add_mission, mission))
        self.monitor.log("out", "mission", "planner", f"queued {mission.mission_id}")
        return mission.mission_id

    def cancel_mission(self, mission_id: str) -> None:
        if not any(m.mission_id == mission_id for m in self.twin.missions):
            raise NotFound(f"There is no mission '{mission_id}'.")
        self._run(partial(self.twin.cancel_mission, mission_id))
        self.monitor.log("out", "mission", "planner", f"cancel {mission_id}", "warn")

    def replan(self) -> None:
        replan = getattr(self.twin, "trigger_global_reassignment", None)
        if not callable(replan):
            raise ApiError("This twin has no global reassignment.")
        self._run(replan)
        self.monitor.log("out", "planner", "fleet", "global reassignment", "warn")

    # -- pairing -------------------------------------------------------------
    def set_linking_mode(self, value: Any) -> str:
        try:
            mode = LinkingMode(value)
        except ValueError:
            raise ApiError(f"Unknown pairing mode '{value}'.") from None
        if not self.twin.link_manager.mode_available(mode):
            raise Conflict("This mode picks from the instance pool, which needs "
                           "instance discovery.")
        self._run(partial(self.twin.set_linking_mode, mode))
        self.monitor.log("out", "pairing", self.twin.name, f"mode set to {mode.value}")
        return mode.value

    def link_pair(self, agent: str, instance: str) -> None:
        links = self.twin.link_manager
        if links.linking_mode is not LinkingMode.GUI:
            raise Conflict("Switch pairing to Manual to choose instances yourself.")
        if agent not in links.unlinked_agents:
            raise NotFound(f"{agent} is not waiting to be paired.")
        if links.unlinked_agents.get(agent) is None:
            raise Conflict(f"Review {agent}'s discovery before pairing it.")
        if links.in_flight(agent):
            raise Conflict(f"{agent} is already in a handshake.")
        if instance not in links.unlinked_instances:
            raise NotFound(f"{instance} is not in the instance pool.")
        if instance not in links.free_instances:
            raise Conflict(f"{instance} is busy with another handshake.")
        self._run(partial(self.twin.gui_assign_instance, agent, instance))
        self.monitor.log("out", "pairing", agent, f"paired by hand with {instance}")

    def release(self, agent: str) -> None:
        if not agent:
            raise ApiError("No agent given.")
        if self.twin.fleet.instance_of(agent) is None:
            raise NotFound(f"{agent} is not linked.")
        self._run(partial(self.twin.release_agent, agent))
        self.monitor.log("out", "instantiate", agent, "release requested", "warn")

    # -- discovery -----------------------------------------------------------
    def resolve_discovery(self, agent: str, choices: dict) -> None:
        layers = pending_layers(self.twin, agent)
        try:
            resolved = apply_resolution(layers, choices)
        except DiscoveryValidationError as exc:
            raise ApiError(str(exc), status=422, fields=exc.errors) from None
        message = discovery_message(resolved)
        # Popped here, not only on the step thread, so the next poll no longer
        # offers it and a second click cannot submit it twice.
        self.twin.discoveries_gui.pop(agent, None)
        self._run(partial(self.twin.gui_trigger_discovery, message))
        self.monitor.log("out", "discovery", agent, "approved from the console", "ok")

    def reject_discovery(self, agent: str) -> None:
        pending_layers(self.twin, agent)
        self.twin.discoveries_gui.pop(agent, None)
        self._run(partial(self.twin.gui_reject_discovery, agent))
        self.monitor.log("out", "discovery", agent, "rejected from the console", "warn")

    def set_autocomplete(self, enabled: bool) -> int:
        """Returns how many waiting discoveries were completed by turning it on."""
        self.twin.autocomplete = bool(enabled)
        completed = 0
        if enabled:
            for agent, layers in list(self.twin.discoveries_gui.items()):
                message = merge_layers(layers, agent)
                self.twin.discoveries_gui.pop(agent, None)
                self._run(partial(self.twin.gui_trigger_discovery, message))
                completed += 1
        self.monitor.log("out", "discovery", self.twin.name,
                         f"auto-complete {'on' if enabled else 'off'}", "warn")
        return completed


def discovery_message(fields: dict[str, Any]) -> AgentDiscoveryMessage:
    fields = dict(fields)
    if isinstance(fields.get("kind"), str):
        fields["kind"] = AgentKind(fields["kind"])
    return AgentDiscoveryMessage(**fields)


def mission_from_spec(spec: dict, taken_ids: set[str]) -> Mission:
    mission_id = (spec.get("mission_id") or "").strip() or f"m{int(time.time() * 1000) % 1000000}"
    if mission_id in taken_ids:
        raise ApiError(f"Mission id '{mission_id}' is already in use.")

    try:
        mission_type = MissionType[spec.get("type", "GOTO_WAYPOINT")]
        posture = MissionPosture[spec.get("posture", "COVERAGE")]
    except KeyError as exc:
        raise ApiError(f"Unknown mission option {exc}.") from None

    goal = spec.get("goal_xy")
    goal_xy = (float(goal[0]), float(goal[1])) if goal else None
    waypoints = [(float(p[0]), float(p[1])) for p in spec.get("waypoints") or []]

    if mission_type in (MissionType.GOTO_WAYPOINT, MissionType.TIME_GATED_GOTO) and goal_xy is None:
        raise ApiError("This mission type needs a goal.")
    if mission_type == MissionType.COVERAGE_PATROL and not waypoints:
        raise ApiError("A patrol needs at least one waypoint.")
    if mission_type == MissionType.TRACK_TARGET and not spec.get("target_id"):
        raise ApiError("Tracking needs a target id.")

    return Mission(
        mission_id=mission_id, mission_type=mission_type, mission_posture=posture,
        goal_xy=goal_xy, waypoints=waypoints,
        unlock_time=float(spec.get("unlock_time") or 0.0),
        target_id=int(spec["target_id"]) if spec.get("target_id") else None,
        battery_budget=float(spec["battery_budget"]) if spec.get("battery_budget") else None,
    )
