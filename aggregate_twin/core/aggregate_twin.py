from __future__ import annotations

import logging
import queue
import time
from typing import Callable

from aggregate_twin.core.discovery_config import check_discovery, load_agent_configs
from aggregate_twin.core.comm_managers.link_manager import LinkingMode, LinkManager, LinkAction
from aggregate_twin.core.comm_managers.mission_manager import MissionManager
from aggregate_twin.core.fleet.fleet_registry import FleetRegistry
from aggregate_twin.core.functions.a_star_custom import AStarPlannerCustom
from aggregate_twin.core.functions.grid_map import GlobalGridMap
from aggregate_twin.core.functions.mission_planner import MissionPlanner
from aggregate_twin.core.obstacle_registry import ObstacleRegistry
from aggregate_twin.core.world_handler import WorldConfig
from aggregate_twin.utils.config_paths import AGENT_CONFIG_FILES_PATH

from core_msgs.global_msgs.global_payloads import (
    AgentDiscoveryMessage, HeartBeatMessage, InstanceDiscoveryMessage
)
from core_msgs.instance_aggregate.mission import Mission, MissionStatus
from core_msgs.instance_aggregate.payloads import ObstacleObservation, TwinStatePayload
from core_msgs.instance_agent.sensor_payloads import PoseMessage
from core_msgs.topic_contract import MessageType
from core_msgs.utils.dispatch import handles, MessageDispatcher


INSTANCE_DISCOVERY = True


class AggregateTwin(MessageDispatcher):
    """Orchestrates multiple agents via instance twins.

    This class owns the loop, the transport and the shared world model. The two flows
    live in their own objects, and are the only places their domain state is written:

        link_manager     agent <-> instance pairing: pools, matching, liveness
        mission_manager  missions: planning, auctioning, status

    Both are HandshakeCoordinators, so they take the same shape; they differ in what a
    link MEANS and which topic it travels on. Neither knows about the transport: they
    append envelopes to an outbox that this class flushes.
    """

    def __init__(
        self,
        world: dict,
        mission_logger,
        namespace: str ,
        name: str ,
        loop_freq: int,
        instance_discovery: bool = INSTANCE_DISCOVERY,
        linking_mode: LinkingMode = LinkingMode.FIRST_FREE,
    ) -> None:

        self.transport = None
        self.logger = logging.getLogger(name)

        self.name = name
        self.namespace = namespace
        self.instance_discovery = instance_discovery

        self.inbox: queue.Queue = queue.Queue()
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()

        # --- shared world model ---
        self.world_config = WorldConfig.from_yaml(world)
        self.fleet = FleetRegistry(clock=self.now)
        self.obstacles = ObstacleRegistry()

        effective_resolution = self.world_config.resolution
        self.grid_map = GlobalGridMap(
            world=self.world_config.specs,
            obstacles=[],
            resolution=effective_resolution,
        )

        self.plan_period = 20        # steps between planning passes
        self.perception_period = 20
        self._sim_step: int = 0
        self.uav_faults: list[dict] = []
        self.loop_freq = loop_freq
        self.dt = 1 / self.loop_freq
        self.bid_timeout = 2.0

        custom_planner = AStarPlannerCustom(self.grid_map)
        self.mission_planner = MissionPlanner(
            astar_planner_custom=custom_planner,
            grid_map=self.grid_map,
            sim_time_fn=lambda: self._sim_step * self.dt,
            uav_world_map_fn=lambda: {a.name: a for a in self.fleet.uavs},
            mission_logger=mission_logger,
        )

        # --- the two flows ---
        self.mission_manager = MissionManager(
            self.name, fleet=self.fleet, mission_planner=self.mission_planner,
            clock=time.time, timeout=self.bid_timeout,
            logger=logging.getLogger(f"{name}.missions"),
        )
        self.link_manager = LinkManager(
            self.name, fleet=self.fleet, clock=time.time,
            action_callback=self.link_manager_callback,
            instance_discovery=instance_discovery, linking_mode=linking_mode,
            timeout=self.bid_timeout, logger=logging.getLogger(f"{name}.links"),
        )

        self.autocomplete = False #True -> use all configs possible to autocomplete the missing robot config
                                 #False -> ask the human first (with options from config or custom)
        self.agent_configs = load_agent_configs(AGENT_CONFIG_FILES_PATH)

        self.logger.debug("Init complete. namespace=%s resolution=%.3f loop_freq=%d mode=%s",
             self.namespace, effective_resolution, loop_freq,
             self.link_manager.linking_mode.value)

    def setup_transport(self, transport):
        self.transport = transport

    # ------------------------------------------------------------------
    # Transport: the only place that publishes
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Drain both managers' outboxes.

        The instantiate channel is mode-dependent, which is why this lives here and
        not in the manager: INSTANTIATE is a globally scoped topic, so a pooled
        instance subscribes to that one and is addressed through the envelope's
        `target`. With instance discovery there is a per-instance ACTIVATE topic.
        """
        if self.transport is None:
            return

        for env in self.link_manager.outbox:
            if self.instance_discovery and env.target is not None:
                self.transport.publish_to_node(env.target, MessageType.ACTIVATE, env)
            else:
                self.transport.publish_global(MessageType.INSTANTIATE, env)
        self.link_manager.outbox.clear()

        for env in self.mission_manager.outbox:
            if env.target is None:
                self.logger.warning("mission envelope with no target, dropping: %s", env.id)
                continue
            self.transport.publish_to_node(env.target, MessageType.MISSION, env)
        self.mission_manager.outbox.clear()

    # ------------------------------------------------------------------
    # Operator / GUI surface
    # ------------------------------------------------------------------

    def submit_command(self, command: Callable[[], None]) -> None:
        """Run `command` on the step thread. Operator writes arrive on other threads,
        and everything they touch is otherwise only written by step()."""
        self._commands.put(command)

    def set_linking_mode(self, mode: LinkingMode) -> LinkingMode:
        mode = self.link_manager.set_linking_mode(mode)
        self._flush()
        return mode

    def gui_reject_discovery(self, agent_name: str) -> None:
        self.link_manager.gui_reject_agent(agent_name)
        self._flush()

    def gui_trigger_discovery(self, discovery_msg: AgentDiscoveryMessage) -> None:
        self.link_manager.gui_confirm_agent(discovery_msg)
        self._flush()

    def gui_assign_instance(self, agent_name: str, instance_name: str) -> None:
        self.link_manager.gui_choose_instance(agent_name, instance_name)
        self._flush()

    def release_agent(self, agent_name: str, reason: str = "released by aggregate") -> None:
        self.link_manager.release(agent_name, reason)
        self._flush()

    def link_agent_instance(self) -> None:
        self.link_manager.link()
        self._flush()

    def add_mission(self, mission: Mission) -> None:
        self.mission_manager.add(mission)

    def assign_mission(self, mission_id: str, agent_name: str) -> None:
        self.mission_manager.assign(mission_id, agent_name)
        self._flush()

    def cancel_mission(self, mission_id: str, reason: str = "cancelled by aggregate") -> None:
        self.mission_manager.cancel_mission(mission_id, reason)
        self._flush()

    def plan_and_auction(self) -> None:
        self.mission_manager.plan_and_elect()
        self._flush()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    @handles(MessageType.DISCOVERY)
    def _handle_discovery(self, _,  msg) -> None:
        if isinstance(msg, InstanceDiscoveryMessage):
            self.link_manager.on_instance_discovered(msg)
            self.logger.debug("Discovery received: instance=%s", msg.name)
            return

        elif isinstance(msg, AgentDiscoveryMessage):
            self.logger.debug("Discovery received: agent=%s", msg.name)
            result = check_discovery(msg, self.agent_configs, self.autocomplete)
            if isinstance(result, AgentDiscoveryMessage):
                self.link_manager.on_agent_discovered(result)
            else:  # dict of questions for the gui
                self.link_manager.on_agent_incomplete(msg.name, result)
            return

        self.logger.warning("unrecognised discovery payload: %r", type(msg).__name__)
        return


    @handles(MessageType.INSTANTIATE)
    def _handle_instantiate_reply(self, _ , env) -> None:
        self.link_manager.route(env)

    @handles(MessageType.ACTIVATE)
    def _handle_activate_reply(self, instance_name: str, env=None) -> None:
        """The same conversation as INSTANTIATE, on the per-instance channel."""
        if env is None:
            instance_name, env = None, instance_name
        self.link_manager.route(env)

    @handles(MessageType.MISSION)
    def _handle_mission_reply(self, instance_name: str, env=None) -> None:
        """Missions arrive on an instance-scoped topic, so they normally carry a source.
        The sourceless form is tolerated: the envelope names its own sender anyway."""
        if env is None:
            instance_name, env = None, instance_name
        self.mission_manager.route(env)

    @handles(MessageType.TWIN_STATE)
    def _handle_twin_state(self, instance_name: str, msg : TwinStatePayload) -> None:
        if not isinstance(msg, TwinStatePayload):
            self.logger.warning("Non TwinStatePayload received, ignoring.")
            return

        # Routed instance -> agent. This also refreshes the instance's liveness clock:
        # twin state IS a bound instance's heartbeat.
        agent_name = self.fleet.agent_of(instance_name)
        if agent_name:
            self.fleet.ingest_twin_state(agent_name, msg)
        else:
            self.logger.warning("TwinStatePayload from unmapped instance=%s, ignoring.",
                                instance_name)

    @handles(MessageType.OBSTACLE)
    def _handle_obstacle(self, instance_name: str, msg : ObstacleObservation) -> None:
        if not isinstance(msg, ObstacleObservation):
            self.logger.warning("Non ObstacleObservation received, ignoring.")
            return

        agent_name = self.fleet.agent_of(instance_name)
        if not agent_name:
            self.logger.warning("Obstacles received from unmapped instance %s", instance_name)
            return

        # Every report, obstacle or agent marker, shows how far the reporter's sensors reach
        entry = self.fleet.get(agent_name)
        if entry is not None:
            entry.observe_detection(msg.x, msg.y)

        # A marker on another agent is not an obstacle but a position fix for that agent
        target = self.fleet.agent_name_of(msg.marker_id) if msg.marker_id is not None else None
        if target and target != agent_name:
            self.send_agent_position(target, msg, observer=agent_name)
            return

        self.obstacles.ingest(agent_name, msg, time.time())


    def send_agent_position(self, agent_name: str, obs: ObstacleObservation, observer: str) -> None:
        """Relay a detection of `agent_name` to its instance as an external pose fix.
        Only what was measured is sent, with the observer's own uncertainty."""
        instance_name = self.fleet.instance_of(agent_name)
        if instance_name is None:
            return
        std = {"x": obs.std_xy, "y": obs.std_xy, "theta": obs.std_theta}
        self.transport.publish_to_node(
            instance_name, MessageType.EXTERNAL_POSE,
            PoseMessage(x=obs.x, y=obs.y, theta=obs.theta, frame_id="world", observer=observer,
                        std={k: v for k, v in std.items() if v is not None},
                        timestamp=obs.timestamp),
        )

    @handles(MessageType.HEARTBEAT)
    def _handle_heartbeat(self, sender: str , msg: HeartBeatMessage) -> None:
        if not self.link_manager.touch(sender):
            self.logger.debug("heartbeat from unknown node=%s", sender)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def step(self) -> None:
        try:
            self._step()
        except Exception:
            self.logger.exception("step failed at sim_step=%d", self._sim_step)

    def _step(self) -> None:
        self._sim_step += 1
        self._drain_inbox()
        self._run_commands()

        if self._sim_step % self.perception_period == 0:
            self.obstacles.prune(time.time())
            self.grid_map.update_perception(self.obstacles.observations())

        if self._sim_step % self.plan_period == 0 or self._sim_step == 1:
            self.mission_manager.plan_and_elect()

        self.link_manager.tick()
        self.mission_manager.tick()
        self.link_manager.sweep()
        self.link_manager.link()
        self._flush()


    def _drain_inbox(self, budget: int = 512) -> None:
        for _ in range(budget):
            try:
                topic, source, msg = self.inbox.get_nowait()
            except queue.Empty:
                self._flush()
                return

            handler_name = self._DISPATCH.get(topic)
            if handler_name is None:
                self.logger.warning("no handler registered for topic %s", topic)
                continue
            handler = getattr(self, handler_name)

            try:
                handler(source, msg)
            except Exception:
                self.logger.exception("handler for %s failed", topic)
            self._flush()

    def _run_commands(self, budget: int = 64) -> None:
        for _ in range(budget):
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                command()
            except Exception:
                self.logger.exception("operator command failed")

    def now(self) -> float:
        return self._sim_step * self.dt

    def link_manager_callback(self, link_action : LinkAction, subject : str) -> None:
        match link_action:
            case LinkAction.SUB_INSTANCE:
                self.transport.subscribe_instance(subject)
            case LinkAction.UNSUB_INSTANCE:
                self.transport.unsubscribe_instance(subject)
            case LinkAction.CONFIRM_INSTANCE:
                self.transport.confirm_node(subject)
            case LinkAction.SUB_AGENT:
                self.transport.subscribe_agent(subject)
            case LinkAction.UNSUB_AGENT:
                self.transport.unsubscribe_agent(subject)
            case LinkAction.CONFIRM_AGENT:
                self.transport.confirm_node(subject)
            case LinkAction.DROP_AGENT:
                self.mission_manager.drop_agent(subject)
            case _:
                self.logger.warning("Unknown link action %s", link_action)

    # ------------------------------------------------------------------
    # Views (delegating: the managers own the state)
    # ------------------------------------------------------------------

    @property
    def linking_mode(self) -> LinkingMode:
        return self.link_manager.linking_mode

    @property
    def unlinked_agents(self):
        return self.link_manager.unlinked_agents

    @property
    def unlinked_instances(self):
        return self.link_manager.unlinked_instances

    @property
    def free_instances(self) -> list[str]:
        return self.link_manager.free_instances

    @property
    def discoveries_gui(self) -> dict:
        return self.link_manager.discoveries_gui

    @property
    def committed(self) -> dict[str, str]:
        return self.mission_manager.committed

    @property
    def missions(self) -> list[Mission]:
        return self.mission_manager.all()

    @property
    def active_missions(self) -> list[Mission]:
        return self.mission_manager.by_status(MissionStatus.ACTIVE)

    @property
    def missions_in_flight(self) -> list[Mission]:
        return self.mission_manager.by_status(MissionStatus.BIDDING)

    @property
    def pending_missions(self) -> list[Mission]:
        return self.mission_manager.by_status(MissionStatus.PENDING)

    def detect_perception_faults(self, observations) -> None:
        """Need to reevaluate"""
        pass

    def trigger_global_reassignment(self) -> None:
        """Need to be reevaluated"""
        pass