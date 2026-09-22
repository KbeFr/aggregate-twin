from __future__ import annotations

import logging
import time
from typing import Callable

from aggregate_twin.core.fleet.agent_entry import AgentEntry

from core_msgs.agents_contract import AgentKind
from core_msgs.global_msgs.global_payloads import AgentDiscoveryMessage
from core_msgs.instance_aggregate.payloads import TwinStatePayload

logger = logging.getLogger(__name__)

# should live in agent_entry ig
DEFAULT_AGENT_RADIUS = 0.22 #[m]

DEFAULT_AGENT_TIMEOUT = 3.0      # [s] no heartbeat from the agent
DEFAULT_INSTANCE_TIMEOUT = 1.5   # [s] no twin state from the instance


class FleetRegistry:
    """
    Shadow state for every known agent, kept current by twin state messages.
    Handles bookkeeping of instance agent links also.

    Liveness of a linked pair is tracked here rather than on the entry, because the
    two halves die separately and for different reasons:
        agent    -> HEARTBEAT from the agent itself      (touch_agent)
        instance -> TWIN_STATE from its twin             (ingest_twin_state)
    """

    def __init__(self, stale_after: float = 3.0,
                 clock: Callable[[], float] = time.time):
        self.stale_after = stale_after
        self.clock = clock
        self._agents: dict[str, AgentEntry] = {}
        self._discovery: dict[str, AgentDiscoveryMessage] = {}
        # Bidirectional routing maps
        self._agent_to_instance: dict[str, str] = {}
        self._instance_to_agent: dict[str, str] = {}
        # Liveness, by source
        self._agent_seen: dict[str, float] = {}       # agent name -> last heartbeat
        self._instance_seen: dict[str, float] = {}    # instance name -> last twin state

    # -- ingestion ----------------------------------------------------------

    def ingest_twin_state(self, agent_name : str , twin_state : TwinStatePayload) -> None:
        agent = self._agents.get(agent_name)
        if agent is None:
            logger.debug("[FleetRegistry] no agent with name %s registered", agent_name)
            return None
        agent.ingest_twin_state(twin_state)

        # Twin state is the instance's heartbeat: it only flows while the twin steps.
        instance = self._agent_to_instance.get(agent_name)
        if instance is not None:
            self._instance_seen[instance] = self.clock()
        return None

    def touch_agent(self, agent_name: str) -> bool:
        """Heartbeat from a linked agent. False if it is not one of ours, so the
        caller can fall through to the unlinked pools."""
        if agent_name not in self._agents:
            return False
        self._agent_seen[agent_name] = self.clock()
        return True

    def register(self, agent_name: str, instance_name, discovery :AgentDiscoveryMessage) -> None:

        if not discovery.kind:
            logger.error("No AgentKind in discovery payload, cannot register %s.", agent_name)
            return
        radius = discovery.radius if discovery.radius else None

        if radius is None:
            logger.error("No AgentRadius in discovery payload, using default radius %s.", DEFAULT_AGENT_RADIUS)
            radius = DEFAULT_AGENT_RADIUS

        entry = self._agents.get(agent_name)
        if entry is None:

            entry = AgentEntry(name=agent_name, kind=discovery.kind, instance_name=instance_name,
                               radius=radius )
            self._agents[agent_name] = entry

        else:
            if entry.instance_name != instance_name:
                logger.info("[FleetRegistry] agent=%s moved %s -> %s",
                                 agent_name, entry.instance_name, instance_name)
                self._instance_to_agent.pop(entry.instance_name, None)
                self._instance_seen.pop(entry.instance_name, None)
            entry.kind = discovery.kind
            entry.instance_name = instance_name
            entry.radius = radius

        self._agent_to_instance[agent_name] = instance_name
        self._instance_to_agent[instance_name] = agent_name
        self._discovery[agent_name] = discovery

        # Start both clocks at the link, or the pair looks stale before the first
        # twin state has had a chance to arrive.
        now = self.clock()
        self._agent_seen[agent_name] = now
        self._instance_seen[instance_name] = now

    def remove(self, agent_name : str) -> None:
        result = self._agents.pop(agent_name, None)
        instance = self._agent_to_instance.pop(agent_name, None)
        self._instance_to_agent.pop(instance, None)
        self._agent_seen.pop(agent_name, None)
        self._instance_seen.pop(instance, None)
        self._discovery.pop(agent_name, None)
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
            agents = [a for a in agents if not self.is_stale(a.name)]
        return agents

    @property
    def available_ugvs(self) -> list[AgentEntry]:
        """UGVs that are live and NOT already running a mission."""
        return [a for a in self.ugvs if not a.mission_id]

    @property
    def ugvs(self) -> list[AgentEntry]:
        return self.all(kind=AgentKind.UGV)

    @property
    def uavs(self) -> list[AgentEntry]:
        return self.all(kind=AgentKind.UAV)

    def is_stale(self, agent_name : str) -> bool:
        """No fresh twin state for this agent. Uses the same clock as stale_instances
        so `all()` and the liveness sweep can never disagree."""
        instance = self._agent_to_instance.get(agent_name)
        last = self._instance_seen.get(instance) if instance else None
        return last is None or self.clock() - last > self.stale_after

    def kind_of(self, agent_name: str, default: AgentKind = AgentKind.UGV) -> AgentKind:
        snap = self._agents.get(agent_name)
        return snap.kind if snap else default

    def agent_of(self, instance_name: str) -> str | None:
        return self._instance_to_agent.get(instance_name)

    def instance_of(self, agent_name: str) -> str | None:
        return self._agent_to_instance.get(agent_name)

    def discovery_of(self, agent_name: str) -> AgentDiscoveryMessage | None:
        """Kept so a released agent can go straight back into the unlinked pool
        without waiting for it to announce itself again."""
        return self._discovery.get(agent_name)

    def instances(self) -> list[str]:
        return list(self._instance_to_agent)

    def amount_linked(self) -> int:
        return len(self._agents)

    # -- liveness of linked pairs ---------------------------------------------

    def stale_agents(self, timeout: float = DEFAULT_AGENT_TIMEOUT) -> list[str]:
        """Linked agents that stopped heartbeating. The twin may still be fine."""
        now = self.clock()
        return [name for name, last in self._agent_seen.items() if now - last > timeout]

    def stale_instances(self, timeout: float = DEFAULT_INSTANCE_TIMEOUT) -> list[str]:
        """Linked agents whose twin stopped publishing state. Returns AGENT names,
        because that is what the handshake is keyed on."""
        now = self.clock()
        return [self._instance_to_agent[inst]
                for inst, last in self._instance_seen.items()
                if now - last > timeout and inst in self._instance_to_agent]