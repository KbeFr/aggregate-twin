from typing import Callable

import pytest

from aggregate_twin.core.comm_managers.link_manager import (
    LinkAction,
    LinkingMode,
    LinkManager,
)
from core.fleet.fleet_registry import FleetRegistry
from core_msgs.agents_contract import AgentKind
from core_msgs.global_msgs.global_payloads import AgentDiscoveryMessage, InstanceDiscoveryMessage, HeartBeatMessage
from core_msgs.instance_aggregate.handshake import HandshakeEnvelope
from core_msgs.instance_aggregate.handshake_shared import HandshakeStatus


# ------------------------------------------------------------------
# Test Doubles / Fakes
# ------------------------------------------------------------------

class FakeClock:
    def __init__(self, start_time: float = 0.0):
        self.current = start_time

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float):
        self.current += seconds



# ------------------------------------------------------------------
# Standard Pytest Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start_time=100.0)


@pytest.fixture
def fleet(clock: FakeClock) -> FleetRegistry:
    return FleetRegistry(clock=clock)

@pytest.fixture
def actions() -> list[tuple[LinkAction, str]]:
    """Captures all actions triggered via action_callback."""
    return []


@pytest.fixture
def action_callback(actions: list[tuple[LinkAction, str]]):
    def _cb(action: LinkAction, name: str):
        actions.append((action, name))
    return _cb


@pytest.fixture
def make_lm(fleet: FleetRegistry, clock: FakeClock, action_callback: Callable):
    """Factory fixture to create LinkManager with arbitrary configurations."""
    def _factory(
        linking_mode: LinkingMode = LinkingMode.POOLED,
        instance_discovery: bool = False,
        timeout: float = 2.0,
    ) -> LinkManager:
        # Default instance_discovery to True when non-pooled modes are requested
        if linking_mode is not LinkingMode.POOLED and not instance_discovery:
            instance_discovery = True
        return LinkManager(
            aggregate_name="agg_node",
            fleet=fleet,
            clock=clock,
            action_callback=action_callback,
            instance_discovery=instance_discovery,
            linking_mode=linking_mode,
            timeout=timeout,
        )
    return _factory


# Convenient preset fixtures:
@pytest.fixture
def lm_pooled(make_lm) -> LinkManager:
    return make_lm(linking_mode=LinkingMode.POOLED, instance_discovery=False)


@pytest.fixture
def lm_first_free(make_lm) -> LinkManager:
    return make_lm(linking_mode=LinkingMode.FIRST_FREE, instance_discovery=True)


@pytest.fixture
def lm_gui(make_lm) -> LinkManager:
    return make_lm(linking_mode=LinkingMode.GUI, instance_discovery=True)


@pytest.fixture
def lm_auction(make_lm) -> LinkManager:
    return make_lm(linking_mode=LinkingMode.AUCTION, instance_discovery=True)

# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestDiscoveryDisabledPooled:
    """Tests for pooled linking where instance discovery is disabled."""

    def test_agent_discovery_triggers_pooled_request(
            self, lm_pooled: LinkManager, fleet: FleetRegistry, actions: list
    ):
        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=0)
        lm_pooled.on_agent_discovered(agent_msg)

        assert actions == [
            (LinkAction.SUB_AGENT, "agent_01"),
            (LinkAction.CONFIRM_AGENT, "agent_01"),
        ]
        assert len(lm_pooled.outbox) == 1

        req_env = lm_pooled.outbox.pop(0)
        assert req_env.id == "agent_01"
        assert req_env.sender == "agg_node"
        assert req_env.target is None
        assert req_env.handshake_status == HandshakeStatus.REQUEST
        assert lm_pooled.in_flight("agent_01") is True

    def test_instance_accept_links_agent_and_instance(
            self, lm_pooled: LinkManager, fleet: FleetRegistry
    ):
        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=0)
        lm_pooled.on_agent_discovered(agent_msg)
        req_env = lm_pooled.outbox.pop(0)

        # Instance accepts with an ACK
        reply_envelope = HandshakeEnvelope(
            id="agent_01",
            sender="instance_A",
            handshake_status=HandshakeStatus.ACK,
            epoch=req_env.epoch,
        )
        lm_pooled.route(reply_envelope)

        assert fleet.agent_of("instance_A") == "agent_01"
        assert fleet.instance_of("agent_01") == "instance_A"
        assert "agent_01" not in lm_pooled.unlinked_agents


class TestDiscoveryEnabledLiveness:
    """Benchmark tests for unlinked pools with instance_discovery=True. Using FREE_FIRST"""

    def test_unlinked_agent_heartbeat_and_staleness_sweep(self, lm_first_free: LinkManager,
                                                          actions: list, clock: FakeClock
    ):
        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=0)
        lm_first_free.on_agent_discovered(agent_msg)

        assert actions == [
            (LinkAction.SUB_AGENT, "agent_01"),
            (LinkAction.CONFIRM_AGENT, "agent_01"),
        ]
        actions.clear()

        assert len(lm_first_free.outbox) == 0
        assert "agent_01" in lm_first_free.unlinked_agents

        lm_first_free.pool_timeout = 5.0
        lm_first_free.touch("agent_01")
        assert not lm_first_free.unlinked_agents.is_stale("agent_01", 5.0)

        # Advance past staleness threshold
        clock.advance(6.0)
        assert lm_first_free.unlinked_agents.is_stale("agent_01", 5.0)

        lm_first_free.sweep()
        assert actions == [(LinkAction.UNSUB_AGENT, "agent_01")]

    def test_unlinked_instance_heartbeat_and_staleness_sweep(self, lm_first_free: LinkManager,
                                                             actions: list, clock: FakeClock
    ):
        instance_msg = InstanceDiscoveryMessage(name="instance_01")
        lm_first_free.on_instance_discovered(instance_msg)

        assert actions == [
            (LinkAction.SUB_INSTANCE, "instance_01"),
            (LinkAction.CONFIRM_INSTANCE, "instance_01"),
        ]
        actions.clear()

        assert len(lm_first_free.outbox) == 0
        assert "instance_01" in lm_first_free.unlinked_instances

        lm_first_free.pool_timeout = 5.0
        lm_first_free.touch("instance_01")
        assert not lm_first_free.unlinked_instances.is_stale("instance_01", 5.0)

        # Advance past staleness threshold
        clock.advance(6.0)
        assert lm_first_free.unlinked_instances.is_stale("instance_01", 5.0)

        lm_first_free.sweep()
        assert actions == [(LinkAction.UNSUB_INSTANCE, "instance_01")]


    def test_instance_rejects_request_returns_to_pool(self, lm_first_free: LinkManager, fleet: FleetRegistry):
        instance_msg = InstanceDiscoveryMessage(name="instance_01")
        lm_first_free.on_instance_discovered(instance_msg)
        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=0)
        lm_first_free.on_agent_discovered(agent_msg)
        req_env = lm_first_free.outbox.pop(0)
        # Instance NACKs
        lm_first_free.route(
            HandshakeEnvelope(
                id="agent_01",
                sender="instance_01",
                handshake_status=HandshakeStatus.NACK,
                epoch=req_env.epoch,
            )
        )
        # Agent should go back to unlinked or retry, instance freed or handled
        assert fleet.agent_of("instance_01") is None
        # TODO make the agent remember which instance already tried and cooldown

    def test_handshake_timeout_recovers_agent(self, lm_first_free: LinkManager, clock: FakeClock):

        lm_first_free.on_instance_discovered(InstanceDiscoveryMessage(name="inst_01"))
        lm_first_free.on_agent_discovered(
            AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=1)
        )
        assert lm_first_free.in_flight("agent_01") is True

        # Advance past initiator timeout (e.g., 5.0s)
        clock.advance(6.0)
        lm_first_free.tick()

        # Should no longer be in flight, and should recover/retry
        assert lm_first_free.in_flight("agent_01") is False


class TestAssignmentModes:
    """Tests targeting specific linking strategies (FIRST_FREE, GUI, AUCTION)."""


    def test_first_free_routes_to_earliest_discovered_instance(self, lm_first_free: LinkManager):
        lm_first_free.on_instance_discovered(
            InstanceDiscoveryMessage(name="instance_01")
        )
        lm_first_free.on_instance_discovered(
            InstanceDiscoveryMessage(name="instance_02")
        )

        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV,agent_id=0)
        lm_first_free.on_agent_discovered(agent_msg)

        assert len(lm_first_free.outbox) == 1
        req_env = lm_first_free.outbox.pop(0)
        assert req_env.id == "agent_01"
        assert req_env.target == "instance_01"
        assert req_env.handshake_status == HandshakeStatus.REQUEST
        assert lm_first_free.in_flight("agent_01") is True


    def test_gui_mode_holds_in_pool_until_explicitly_assigned(self, lm_gui: LinkManager):
        lm_gui.on_instance_discovered(
            InstanceDiscoveryMessage(name="instance_01")
        )
        lm_gui.on_instance_discovered(
            InstanceDiscoveryMessage(name="instance_02")
        )

        agent_msg = AgentDiscoveryMessage(name="agent_01", kind=AgentKind.UGV, agent_id=0)
        lm_gui.on_agent_discovered(agent_msg)

        # Must stay buffered until GUI operator picks an instance
        assert "agent_01" in lm_gui.unlinked_agents
        assert len(lm_gui.outbox) == 0

        lm_gui.gui_choose_instance("agent_01", "instance_02")

        assert len(lm_gui.outbox) == 1
        req_env = lm_gui.outbox.pop(0)
        assert req_env.id == "agent_01"
        assert req_env.target == "instance_02"
        assert req_env.handshake_status == HandshakeStatus.REQUEST
        assert lm_gui.in_flight("agent_01") is True

