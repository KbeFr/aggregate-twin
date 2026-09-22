"""
link_manager.py

The whole agent <-> instance pairing flow: discovery, the pools, choosing who gets
asked, and the liveness that ends a pairing.

"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Callable

from aggregate_twin.core.comm_managers.handshake_coordinator import HandshakeCoordinator
from aggregate_twin.core.fleet.unlinked_registry import UnlinkedRegistry

from core_msgs.global_msgs.global_payloads import (
    AgentDiscoveryMessage, InstanceDiscoveryMessage,
)

logger = logging.getLogger(__name__)



class LinkingMode(str, Enum):
    """How an unlinked agent is matched with an instance."""
    POOLED = "pooled"           # broadcast, first ACK wins
    FIRST_FREE = "first_free"   # directed at the longest-waiting free instance
    GUI = "gui"                 # directed at whatever the operator picked
    AUCTION = "auction"         # ask every free instance for a bid (an election)


#TODO can be just name based, no agent or instance -> check transport
class LinkAction(str, Enum):
    SUB_INSTANCE = "subscribe_instance"
    UNSUB_INSTANCE = "unsubscribe_instance"
    CONFIRM_INSTANCE = "confirm_instance"
    SUB_AGENT = "subscribe_agent"
    UNSUB_AGENT = "unsubscribe_agent"
    CONFIRM_AGENT = "confirm_agent"
    DROP_AGENT = "drop_agent"


class LinkManager(HandshakeCoordinator):
    """Subjects are AGENT names. An instance is never a subject, only a receiver."""

    kind = "agent"

    def __init__(self,
                 aggregate_name: str,
                 fleet,
                 clock,
                 action_callback: Callable[[LinkAction, str], None],
                 instance_discovery: bool = False,
                 linking_mode: LinkingMode = LinkingMode.POOLED,
                 timeout: float = 2.0,
                 logger: logging.Logger | None = None,
                 ) -> None:
        super().__init__(aggregate_name, clock=clock, timeout=timeout, logger=logger)

        self.fleet = fleet
        self.action_callback = action_callback

        self.unlinked_agents = UnlinkedRegistry(clock=clock)
        self.unlinked_instances = UnlinkedRegistry(clock=clock)

        self.instance_discovery = instance_discovery
        self.linking_mode = self._supported(linking_mode)

        self.instance_choice: dict[str, str] = {}   # agent -> instance, from the GUI
        self.discoveries_gui: dict = {}             # discoveries the operator must complete

        # instance or agent in need for double check when the other timed out in link
        self.cooldown_check : set = set()

        self.pool_timeout = 10.0                     # no heartbeat from an unlinked node
        self.agent_timeout = 10.0                    # no heartbeat from a linked agent
        self.instance_timeout = 10.0                 # no twin state from a linked instance

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def on_agent_discovered(self, discovery: AgentDiscoveryMessage) -> None:
        """A complete, usable agent spec. Incomplete ones go to the GUI first."""
        agent_name = discovery.name
        if self._already_handled(agent_name):
            return
        self.unlinked_agents.register(agent_name, discovery)
        self.agent_sub_confirm(agent_name)
        self.link()

    def on_agent_incomplete(self, agent_name: str, questions: dict) -> None:
        """Known, but not linkable until someone fills in the missing config."""
        self.unlinked_agents.register(agent_name, None)
        self.agent_sub_confirm(agent_name)
        self.discoveries_gui.update(questions)

    def agent_sub_confirm(self, agent_name : str):
        self.action_callback(LinkAction.SUB_AGENT, agent_name)
        self.action_callback(LinkAction.CONFIRM_AGENT, agent_name)

    def on_instance_discovered(self, discovery: InstanceDiscoveryMessage) -> None:
        """An instance announcing itself joins the pool of candidates. It does not
        start a handshake: the subject of a pairing is always the agent."""
        instance_name = discovery.name
        if self.fleet.agent_of(instance_name):
            self.logger.debug("discovery from linked instance=%s, ignoring", instance_name)
            return

        if instance_name not in self.unlinked_instances:
            self.action_callback(LinkAction.SUB_INSTANCE, instance_name)
            self.logger.debug("instance=%s joined the pool", instance_name)
        self.unlinked_instances.register(instance_name, discovery)

        # Tell it we know: it stops discovery and starts heartbeating instead.
        self.action_callback(LinkAction.CONFIRM_INSTANCE, instance_name)
        self.link()

    def gui_confirm_agent(self, discovery: AgentDiscoveryMessage) -> None:
        """The operator completed a discovery. Same guard as the normal path: a double
        click, or a confirm that races a link, must not pool an agent that is linked."""
        agent_name = discovery.name
        self.discoveries_gui.pop(agent_name, None)
        if self._already_handled(agent_name):
            return
        self.unlinked_agents.register(agent_name, discovery)
        self.link()

    def gui_choose_instance(self, agent_name: str, instance_name: str) -> None:
        self.instance_choice[agent_name] = instance_name
        self.link()

    def gui_reject_agent(self, agent_name: str) -> None:
        """The operator refused an incomplete discovery: same exit as an eviction."""
        self.discoveries_gui.pop(agent_name, None)
        if agent_name in self.unlinked_agents and self.unlinked_agents.get(agent_name) is None:
            self.unlinked_agents.remove(agent_name)
            self.action_callback(LinkAction.UNSUB_AGENT, agent_name)
        self.forget(agent_name)

    def mode_available(self, mode: LinkingMode) -> bool:
        """Every mode but POOLED picks from the instance pool, which only fills up
        when instances announce themselves."""
        return self.instance_discovery or mode is LinkingMode.POOLED

    def set_linking_mode(self, mode: LinkingMode) -> LinkingMode:
        mode = self._supported(mode)
        if mode is not LinkingMode.GUI:
            self.instance_choice.clear()        # a pick only means something in GUI mode
        self.linking_mode = mode
        self.link()
        return mode

    def _supported(self, mode: LinkingMode) -> LinkingMode:
        if self.mode_available(mode):
            return mode
        self.logger.warning("linking_mode=%s needs instance discovery, using POOLED", mode.value)
        return LinkingMode.POOLED

    def _already_handled(self, agent_name: str) -> bool:
        if self.fleet.instance_of(agent_name):
            self.logger.debug("agent=%s ignored (already linked)", agent_name)
            return True
        if self.in_flight(agent_name):
            self.logger.debug("agent=%s ignored (handshake in flight)", agent_name)
            return True
        return False

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def link(self) -> None:
        """Give every waiting agent an instance. Cheap to call on every discovery and
        every step: an agent already in flight is skipped."""
        for agent_name, discovery in list(self.unlinked_agents):
            if discovery is None or self.in_flight(agent_name):
                continue                        # waiting on the GUI, or in flight

            if self.linking_mode is LinkingMode.POOLED:
                self.request(agent_name, payload=discovery)
                continue

            free = self.free_instances
            if not free:
                return                          # nothing to hand out this pass

            if self.linking_mode is LinkingMode.GUI:
                target = self.instance_choice.get(agent_name)
                if target in free:
                    self.instance_choice.pop(agent_name, None)
                    self.request(agent_name, payload=discovery, target=target)
            elif self.linking_mode is LinkingMode.AUCTION:
                hints = {inst: self.link_hint(inst, discovery) for inst in free}
                self.elect(agent_name, hints, self.pick_instance, payload=discovery)
            elif self.linking_mode is LinkingMode.FIRST_FREE:
                self.request(agent_name, payload=discovery, target=free[0])

    def link_hint(self, instance_name: str, discovery: AgentDiscoveryMessage):
        """What an instance bids on when pairing is auctioned. Returning None would
        make it a plain accept, so give it something real: host load, GPU, locality."""
        return {"agent": discovery.name, "instance": instance_name}

    def pick_instance(self, discovery, bids: dict, hints: dict) -> str | None:
        """Default: the lightest-loaded offer. Override for anything smarter."""
        offers = {i: b for i, b in bids.items() if b is not None}
        if not offers:
            return None

        def load(instance):
            bid = offers[instance]
            value = bid.get("load") if isinstance(bid, dict) else getattr(bid, "load", None)
            return value if value is not None else 0.0

        return min(offers, key=load)

    def release(self, agent_name: str, reason: str = "released by aggregate") -> None:
        self.cancel(agent_name, reason)

    # ------------------------------------------------------------------
    # What a pairing means
    # ------------------------------------------------------------------

    def on_link(self, agent_name: str, instance_name: str) -> None:
        if self.fleet.agent_of(instance_name):
            self.logger.warning("instance=%s confirmed again while live, ignoring",
                                instance_name)
            return

        discovery = self.unlinked_agents.remove(agent_name)
        if discovery is None:
            self.logger.error("No discovery message found for agent %s.", agent_name)
            return

        self.unlinked_instances.remove(instance_name)
        self.fleet.register(agent_name, instance_name, discovery)
        self.logger.info("Confirmed: agent=%s kind=%s instance=%s",
                         agent_name, discovery.kind, instance_name)

    def on_release(self, agent_name: str, instance_name: str | None) -> None:
        discovery = self.fleet.discovery_of(agent_name)
        instance_name = instance_name or self.fleet.instance_of(agent_name)

        # Ask BEFORE the entry goes away: if the agent is the half that died, putting
        # it back in the pool would just re-link it to the next instance and lose that
        # one too. If the twin died instead, the agent is fine and should be re-linked.
        agent_alive = (discovery is not None
                       and agent_name not in self.fleet.stale_agents(self.agent_timeout))

        self.fleet.remove(agent_name)
        self.action_callback(LinkAction.DROP_AGENT, agent_name)

        if instance_name:
            self.action_callback(LinkAction.UNSUB_INSTANCE, instance_name)

        if agent_alive:
            self.unlinked_agents.register(agent_name, discovery)
        else:
            self.unlinked_agents.remove(agent_name)
            self.logger.info("agent=%s not re-pooled: no recent heartbeat", agent_name)

        self.logger.info("Released agent=%s from instance=%s", agent_name, instance_name)

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def touch(self, name: str) -> bool:
        """A heartbeat. Most specific first: a linked agent, then the two pools.
        False means nobody here knows that name."""
        if self.fleet.touch_agent(name):
            return True
        if self.unlinked_agents.touch(name):
            return True
        if self.unlinked_instances.touch(name):
            return True
        if self.fleet.agent_of(name):
            self.logger.debug("heartbeat from linked instance=%s (should be paused)", name)
            return True
        return False

    def sweep(self) -> None:
        """Heartbeats for unlinked nodes, twin state for linked instances, heartbeats
        for linked agents. Each death ends as a cancel on the handshake."""
        for name in self.unlinked_agents.stale_names(self.pool_timeout):
            self.unlinked_agents.remove(name)
            self.discoveries_gui.pop(name, None)
            self._abandon_agent(name, "agent heartbeat timeout")
            self.action_callback(LinkAction.UNSUB_AGENT,name)
            self.logger.info("evicted unlinked agent=%s", name)

        for name in self.unlinked_instances.stale_names(self.pool_timeout):
            self.unlinked_instances.remove(name)
            self._abandon_receiver(name, "instance heartbeat timeout")
            self.action_callback(LinkAction.UNSUB_INSTANCE,name)
            self.logger.info("evicted unlinked instance=%s", name)

        for agent_name in self.fleet.stale_instances(self.instance_timeout):
            self.cooldown_check.add(agent_name)
            self._release_pair(agent_name, "no twin state")

        for agent_name in self.fleet.stale_agents(self.agent_timeout):
            self._release_pair(agent_name, "no agent heartbeat")

    def _release_pair(self, agent_name: str, reason: str) -> None:
        if not self.in_flight(agent_name) or self.releasing(agent_name):
            return
        self.logger.warning("releasing %s: %s", agent_name, reason)
        self.cancel(agent_name, reason)

    def _abandon_agent(self, agent_name: str, reason: str) -> None:
        logger.debug("abandoning %s", agent_name)

        if self.in_flight(agent_name):
            self.cancel(agent_name, reason)
        # TODO what if agent comes back after some time, it only knows heartbeat
        self.forget(agent_name)

    def _abandon_receiver(self, instance_name: str, reason: str) -> None:
        """The instance died, so every agent it was engaged on has to let go. Bidders
        count: a candidate that never answers would hold the election open."""
        logger.debug("abandoning %s", instance_name)
        for agent_name in self.subjects_on(instance_name):
            self.cancel(agent_name, reason)
        # TODO what if instance comes back after some time, it only knows heartbeat


    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @property
    def free_instances(self) -> list[str]:
        """Pooled instances that are neither linked nor promised to a handshake.
        `engaged_instances` also covers open bids and releases that have not been
        acknowledged, so a candidate cannot be handed out twice."""
        taken = self.engaged_instances()
        taken.update(self.fleet.instances())
        return [name for name in self.unlinked_instances.names() if name not in taken]