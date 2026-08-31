"""
fleet_registry.py
"""
from __future__ import annotations

import logging

from agent_entry import AgentEntry
from core_msgs.agents_contract import AgentKind
from core_msgs.instance_aggregate.payloads import TwinStatePayload

logger = logging.getLogger(__name__)

# should live in agent_entry ig
DEFAULT_AGENT_RADIUS = 0.22 #[m]

class FleetRegistry:
    """Shadow state for every known agent, kept current by telemetry only."""

    def __init__(self, stale_after: float = 3.0):
        self.stale_after = stale_after
        self._agents: dict[str, AgentEntry] = {}
        # Bi-directional routing maps
        self._agent_to_instance: dict[str, str] = {}
        self._instance_to_agent: dict[str, str] = {}

    # -- ingestion ----------------------------------------------------------

    def ingest_twin_state(self, agent_name : str , twin_state : TwinStatePayload) -> None:
        agent = self._agents.get(agent_name)
        if agent is None:
            logger.debug("[FleetRegistry] no agent with name %s registered", agent_name)
            return None
        agent.ingest_twin_state(twin_state)
        return None

    def register(self, agent_name: str, kind: AgentKind, instance_name: str,
                 radius: float | None = None) -> None:
        entry = self._agents.get(agent_name)
        if entry is None:
            entry = AgentEntry(name=agent_name, kind=kind, instance_name=instance_name, radius=radius)
            self._agents[agent_name] = entry
        else:
            if entry.instance_name != instance_name:
                logger.info("[FleetRegistry] agent=%s moved %s -> %s",
                                 agent_name, entry.instance_name, instance_name)
                self._instance_to_agent.pop(entry.instance_name, None)
            entry.kind = kind
            entry.instance_name = instance_name
        if radius is not None:
            entry.radius = radius

        self._agent_to_instance[agent_name] = instance_name
        self._instance_to_agent[instance_name] = agent_name


    def remove(self, agent_name : str) -> None:
        result = self._agents.pop(agent_name, None)
        instance = self._agent_to_instance.pop(agent_name, None)
        self._instance_to_agent.pop(instance, None)
        if result is None:
            logger.debug("[FleetRegistry] no agent with name %s registered", agent_name)


    # -- queries --------------------------------------------------------------

    def get(self, agent_id: str) -> AgentEntry | None:
        return self._agents.get(agent_id)

    def all(self, kind: AgentKind | None = None, include_stale: bool = False) -> list[AgentEntry]:
        agents = list(self._agents.values())
        if kind is not None:
            agents = [a for a in agents if a.kind == kind]
        if not include_stale:
            agents = [a for a in agents if a.age <= self.stale_after]
        return agents

    @property
    def ugvs(self) -> list[AgentEntry]:
        return self.all(kind=AgentKind.UGV)

    @property
    def uavs(self) -> list[AgentEntry]:
        return self.all(kind=AgentKind.UAV)

    def is_stale(self, agent_id: str) -> bool:
        snap = self._agents.get(agent_id)
        return snap is None or snap.age > self.stale_after

    def kind_of(self, agent_id: str, default: AgentKind = AgentKind.UGV) -> AgentKind:
        snap = self._agents.get(agent_id)
        return snap.kind if snap else default


    def agent_of(self, instance_name: str) -> str | None:
        return self._instance_to_agent.get(instance_name)

    def instance_of(self, agent_name: str) -> str | None:
        return self._agent_to_instance.get(agent_name)

    def amount_linked(self) -> int:
        return len(self._agents)