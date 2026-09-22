"""
mission_manager.py

The whole mission flow: what exists, what is planned, who is bidding, and what a
mission's status is at any moment.

The only place MissionStatus is written. Everything else asks.
"""
from __future__ import annotations

import logging

from aggregate_twin.core.comm_managers.handshake_coordinator import HandshakeCoordinator

from core_msgs.instance_aggregate.mission import Mission, MissionStatus
from core_msgs.instance_aggregate.mission_handshake import (
    MISSION_ORPHAN_REPLIES, MissionInitiator,
)


class MissionManager(HandshakeCoordinator):
    """Subjects are MISSION ids. Receivers are instances, but the planner speaks agent
    names, so this class is also the translation layer between the two."""

    kind = "mission"
    initiator_cls = MissionInitiator
    orphan_replies = MISSION_ORPHAN_REPLIES

    def __init__(self,
                 aggregate_name: str,
                 fleet,
                 mission_planner,
                 clock,
                 timeout: float = 2.0,
                 logger: logging.Logger | None = None,
                 ) -> None:
        super().__init__(aggregate_name, clock=clock, timeout=timeout, logger=logger)
        self.fleet = fleet
        self.mission_planner = mission_planner
        self.missions: dict[str, Mission] = {}

    # ------------------------------------------------------------------
    # The catalogue
    # ------------------------------------------------------------------

    def remove(self, mission_id: str) -> None:
        """Drop a mission from the catalogue for good."""
        if self.in_flight(mission_id):
            self.cancel(mission_id, "mission removed")
        self.missions.pop(mission_id, None)
        self.forget(mission_id)

    def add(self, mission: Mission) -> None:
        if mission.mission_id in self.missions:
            self.logger.warning("Adding mission that is already present: %s",
                                mission.mission_id)
            return
        self.missions[mission.mission_id] = mission

    def get(self, mission_id: str) -> Mission | None:
        return self.missions.get(mission_id)

    def all(self) -> list[Mission]:
        return list(self.missions.values())

    def by_status(self, status: MissionStatus) -> list[Mission]:
        return [m for m in self.missions.values() if m.mission_status is status]

    @property
    def committed(self) -> dict[str, str]:
        """{agent: mission_id} for every mission awarded or active."""
        return {agent: mission_id
                for instance, mission_id in self.committed_receivers().items()
                for agent in [self.fleet.agent_of(instance)] if agent}

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    def plan_and_elect(self) -> None:
        eligible = [m for m in self.missions.values()
                    if m.mission_status is MissionStatus.PENDING
                    and not self.in_flight(m.mission_id)]
        if not eligible:
            return

        for mission, hints in self.mission_planner.assign_and_plan(
                pending_missions=eligible, available_agents=self.fleet.available_ugvs):

            inst_hints = {self.fleet.instance_of(a): h for a, h in hints.items()
                          if self.fleet.instance_of(a)}
            if not inst_hints:
                self.logger.debug("no instance for mission=%s", mission.mission_id)
                continue

            if self.elect(mission.mission_id, inst_hints, self._winner, payload=mission):
                mission.mission_status = MissionStatus.BIDDING

    def assign(self, mission_id: str, agent_name: str) -> None:
        """Directed: no bidding, the agent's instance just gets it."""
        mission = self.missions.get(mission_id)
        instance_name = self.fleet.instance_of(agent_name)
        if mission is None or instance_name is None:
            self.logger.warning("cannot assign mission=%s to agent=%s", mission_id, agent_name)
            return
        if self.request(mission_id, payload=mission, target=instance_name):
            mission.mission_status = MissionStatus.BIDDING

    def cancel_mission(self, mission_id: str, reason: str = "cancelled by aggregate") -> None:
        mission = self.missions.get(mission_id)
        if mission is not None:
            mission.mission_status = MissionStatus.CANCELLED
        self.cancel(mission_id, reason)

    def drop_agent(self, agent_name: str) -> None:
        """The agent lost its instance, so it takes its missions with it."""
        for mission_id, mission in list(self.missions.items()):
            if mission.assigned_ugv != agent_name:
                continue
            if self.in_flight(mission_id):
                self.cancel(mission_id, "agent released")
            mission.assigned_ugv = None
            mission.mission_status = MissionStatus.PENDING

    def _winner(self, mission, bids: dict, hints: dict) -> str | None:
        """Adapts the planner (agent names) to the handshake (instance names)."""
        by_agent = {self.fleet.agent_of(i): b for i, b in bids.items()
                    if self.fleet.agent_of(i)}
        hints_by_agent = {self.fleet.agent_of(i): h for i, h in hints.items()
                          if self.fleet.agent_of(i)}
        agent = self.mission_planner.get_winner(mission, by_agent, hints_by_agent)
        if agent is None:
            self.logger.debug("election failed mission=%s", mission.mission_id)
            return None
        return self.fleet.instance_of(agent)

    # ------------------------------------------------------------------
    # What a mission assignment means
    # ------------------------------------------------------------------

    def on_link(self, mission_id: str, instance_name: str) -> None:
        mission = self.missions.get(mission_id)
        if mission is None:
            return
        mission.assigned_ugv = self.fleet.agent_of(instance_name)
        mission.mission_status = MissionStatus.ACTIVE

        hint = self.hint_of(mission_id)         # None when it was assigned directly
        if hint is not None:
            mission.set_path(getattr(hint, "path", None))
        self.logger.info("mission=%s ACTIVE on %s", mission_id, mission.assigned_ugv)

    def on_complete(self, mission_id: str, instance_name: str | None) -> None:
        mission = self.missions.get(mission_id)
        if mission is None:
            return
        mission.mission_status = MissionStatus.COMPLETE
        self.logger.info("mission=%s COMPLETE", mission_id)

    def on_release(self, mission_id: str, instance_name: str | None) -> None:
        mission = self.missions.get(mission_id)
        if mission is None:
            return
        mission.assigned_ugv = None
        if mission.mission_status is not MissionStatus.CANCELLED:
            mission.mission_status = MissionStatus.PENDING     # re-planned next pass
        self.logger.info("mission=%s released", mission_id)

    def on_retire(self, mission_id: str) -> None:
        """An attempt that ended without ever going ACTIVE has to go back to PENDING,
        or it sits in BIDDING forever and plan_and_elect never looks at it again."""
        mission = self.missions.get(mission_id)
        if mission is not None and mission.mission_status is MissionStatus.BIDDING:
            self.logger.debug("mission=%s attempt ended with no winner, back to PENDING",
                              mission_id)
            mission.assigned_ugv = None
            mission.mission_status = MissionStatus.PENDING