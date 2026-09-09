from __future__ import annotations

import logging
import queue
from dataclasses import fields
from pathlib import Path
from typing import Any

import omegaconf
from omegaconf import OmegaConf, ListConfig, MissingMandatoryValue

from core_msgs.topic_contract import MessageType
from core_msgs.global_msgs.global_payloads import DiscoveryMessage
from core_msgs.instance_aggregate.payloads import ObstacleObservation, TwinStatePayload
from core_msgs.instance_aggregate.instantiate_handshake import InstantiateInitiator, InstantiateEnvelope, \
    InstantiateAction
from core_msgs.instance_aggregate.mission_handshake import MissionSession,  MissionEnvelope, SessionState
from core_msgs.instance_aggregate.mission import Mission, MissionStatus
from core_msgs.utils.dispatch import handles, MessageDispatcher

from core.functions.a_star_custom import AStarPlannerCustom
from core.obstacle_registry import ObstacleRegistry

from core.world_handler import WorldConfig
from core.fleet.fleet_registry import FleetRegistry
from core.functions.grid_map import GlobalGridMap
from core.functions.mission_planner import MissionPlanner
from core_msgs.utils.utils import load_config

AGENT_CONFIG_PATH = "config/agent_configs"

class AggregateTwin(MessageDispatcher):
    """
    Orchestrates multiple agents via instance twins using a decoupled
    Initiator pattern for lifecycle and mission handshakes.
    """
    def __init__(
        self,
        world: dict,
        mission_logger,
        namespace: str ,
        name: str ,
        loop_freq: int,
    ) -> None:

        self.transport = None
        self.logger = logging.getLogger(name)

        self.name = name
        self.namespace = namespace

        self.inbox: queue.Queue = queue.Queue()

        # --- Domain State ---
        self.world_config = WorldConfig.from_yaml(world)
        self.fleet = FleetRegistry()
        self.obstacles = ObstacleRegistry()


        effective_resolution = self.world_config.resolution
        self.grid_map = GlobalGridMap(
            world=self.world_config.specs,
            obstacles=[],
            resolution=effective_resolution,
        )

        self._missions: dict[str, Mission] = {}      # every mission, any status
        self._mission_sessions: dict[str, MissionSession] = {}
        self.award_backoff_steps = 50  # wait before re-auctioning a failed mission
        self.max_attempts = 4
        self.bid_timeout = 2

        self.plan_period = 20        # steps between planning passes
        self.perception_period = 20
        self._sim_step: int = 0
        self.uav_faults: list[dict] = []
        self.loop_freq = loop_freq
        self.dt = 1 / self.loop_freq

        custom_planner = AStarPlannerCustom(self.grid_map)

        self.mission_planner = MissionPlanner(
            astar_planner_custom=custom_planner,
            grid_map=self.grid_map,
            sim_time_fn=lambda: self._sim_step * self.dt,
            uav_world_map_fn=lambda: {a.name: a for a in self.fleet.uavs},
            mission_logger=mission_logger,
        )


        # --- Protocol & Topology State ---
        self._instantiate_initiators: dict[str, InstantiateInitiator] = {}

        self._pending_discovery: dict[str, DiscoveryMessage] = {}
        self._instance_obstacles: dict[str, ObstacleObservation] = {}

        self.discoveries_gui : dict = {}
        self.autocomplete = False #True -> use all configs possible to autocomplete the missing robot config
                                 #False -> ask the human first (with options from config or custom)
        self.agent_configs = self.load_agent_configs(AGENT_CONFIG_PATH)

        self.logger.debug("Init complete. namespace=%s resolution=%.3f loop_freq=%d",
             self.namespace, effective_resolution, loop_freq)


    def setup_transport(self, transport):
        self.transport = transport


    def load_agent_configs(self, path : str | Path ) -> dict:
        agent_config = {}
        # go through all the folders in AGENT_CONFIG_PATH, then the files in them and store them
        #dict[folder_first_work][file_first_word] = config.yaml

        for item in Path(path).iterdir():
            if item.is_dir():
                agent_config[item.name.split("_")[0]] = self.load_agent_configs(item)
            elif item.is_file():
                agent_config[item.name.split("__")[0]] = load_config(item)
        return agent_config



    # ------------------------------------------------------------------
    # Dispatching
    # ------------------------------------------------------------------

    def _request_instance(self, agent_name: str, discovery_msg: DiscoveryMessage) -> None:
        """Sends request envelope to the global INSTANTIATE topic for instance initiation"""

        existing = self._instantiate_initiators.get(agent_name)
        if existing is not None and existing.confirmed:
            self.logger.debug("Instantiating handshake already confirmed for agent=%s", agent_name)
            return

        self.logger.debug("Requesting instance for agent=%s", agent_name)

        initiator = InstantiateInitiator(agent_name, self.name)
        self._instantiate_initiators[agent_name] = initiator

        self.transport.publish_global(MessageType.INSTANTIATE, initiator.request(discovery_msg))


    def _release_instance(self, agent_name: str) -> None:
        """Releases the instance from its agent coupling, setting it unconfigured again"""

        initiator = self._instantiate_initiators.get(agent_name)
        if not initiator or not initiator.confirmed:
            self.logger.warning("Cannot release %s: No live instance.", agent_name)
            return

        self.logger.debug("Releasing instance for agent=%s", agent_name)

        self.transport.publish_to_instance(self.fleet.instance_of(agent_name), MessageType.INSTANTIATE, initiator.cancel())



    def cancel_mission(self, mission_id: str, reason: str = "cancelled by aggregate") -> None:
        session = self._mission_sessions.get(mission_id)
        if session is None:
            self.logger.warning("cannot cancel mission=%s: no live session", mission_id)
            return
        self.logger.debug("cancelling mission=%s on %s", mission_id, session.committed_agent)
        self._send_session_out(session.cancel(reason))



    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    @handles(MessageType.DISCOVERY)
    def _handle_discovery(self, msg: DiscoveryMessage) -> None:

        agent_name = msg.agent_name
        if not agent_name or self.fleet.instance_of(agent_name):
            self.logger.debug("Ignoring discovery for agent=%s (already live)", agent_name)
            return

        initiator = self._instantiate_initiators.get(agent_name)
        if initiator is not None and initiator.confirmed:
            self.logger.debug("Ignoring discovery for agent=%s (instantiate already confirmed)", agent_name)
            return

        self.logger.debug("Discovery received: agent=%s", agent_name)

        discovery_msg = self.check_discovery(msg)
        if discovery_msg is not None:
            self._pending_discovery[agent_name] = discovery_msg
            self._request_instance(agent_name, discovery_msg)


    def gui_trigger_discovery(self, discovery_msg: DiscoveryMessage) -> None:
        """ Called from gui when discovery is confirmed """
        agent_name = discovery_msg.agent_name
        self._pending_discovery[agent_name] = discovery_msg
        self._request_instance(agent_name, discovery_msg)

    def check_discovery(self, msg : DiscoveryMessage) -> DiscoveryMessage | None:

        layers: dict[str, Any] = {"reported": self.structured(msg)}

        agent_kind = msg.kind.value  # ugv, uav

        # layer 1 -> on agent_name level
        agent_specific = self.agent_configs.get("agent", {}).get(msg.agent_name)
        if agent_specific is not None:
            layers["agent"] = OmegaConf.create(agent_specific)

        # layer 2 -> on agent_type level (based on kind)
        type_specific = self.agent_configs.get(agent_kind, {}).get(msg.agent_type)
        if type_specific is not None:
            layers["type"] = OmegaConf.create(type_specific)

        # layer 3 -> default kind configs
        default_kind = self.agent_configs.get(agent_kind, {}).get("default")
        if default_kind is not None:
            layers["kind"] = OmegaConf.create(default_kind)

        if self.autocomplete:
            # lowest priority first, so later entries win the merge:
            # kind default < type default < agent-specific < self-reported
            ordered = [layers[k] for k in ("kind", "type", "agent", "reported") if k in layers]
            disc = OmegaConf.merge(*ordered)

            # can check the missing still, but will just log and not act on it for now
            container = OmegaConf.to_container(disc, throw_on_missing=False)
            missing = [k for k, v in container.items() if v == "???"]
            if missing:
                self.logger.warning(
                    "agent=%s: no config layer (or self-report) could fill %s, leaving as None",
                    msg.agent_name, ", ".join(missing))
                for k in missing:
                    container[k] = None

            return DiscoveryMessage(**container)

        else:
            # here the gui will first ask human intervention and validation of discovery
            self.discoveries_gui[msg.agent_name] = layers
            return None  # wait for gui to trigger

    @staticmethod
    def structured(obj):
        """ omegaconf only treats "???" as not filled in for some reason """
        import copy
        obj = copy.copy(obj)  # don't mutate the caller's live DiscoveryMessage
        for field in fields(obj.__class__):
            value = getattr(obj, field.name)
            setattr(obj, field.name, "???" if value is None else AggregateTwin._sanitize(value))
        return OmegaConf.structured(obj)

    @staticmethod
    def _sanitize(value):
        """Recursively swap numpy scalars/arrays for native Python types"""
        if isinstance(value, dict):
            return {k: AggregateTwin._sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [AggregateTwin._sanitize(v) for v in value]
        tolist = getattr(value, "tolist", None)
        if callable(tolist) and not isinstance(value, (str, bytes)):
            try:
                return AggregateTwin._sanitize(tolist())
            except Exception:
                pass
        return value

    @handles(MessageType.INSTANTIATE)
    def _handle_instantiate_reply(self, env: InstantiateEnvelope) -> None:

        # feedback message protection (inout)
        if env.sender == self.name:
            return

        agent_name = env.agent_name

        initiator = self._instantiate_initiators.get(agent_name)
        if not initiator:
            return

        # Let the initiator resolve the protocol rules
        result = initiator.handle(env)

        # The reply from this is always for global comm
        if result.reply:
            self.transport.publish_global(MessageType.INSTANTIATE, result.reply)

        # Dispatch to dedicated handlers
        if result.action == InstantiateAction.LINK_AGENT:
            self._on_instance_confirmed(agent_name, env.sender)
        elif result.action == InstantiateAction.RELEASE_AGENT:
            self._on_instance_released(agent_name, env.sender)


    def _on_instance_confirmed(self, agent_name: str, instance_name: str) -> None:
        """Handles the 'confirmed' state transition."""
        if self.fleet.agent_of(instance_name):
            self.logger.warning("Agent=%s confirmed again while already live, ignoring.", agent_name)
            return

        discovery = self._pending_discovery.pop(agent_name, None)

        if not discovery:
            self.logger.error("No Discovery message found for agent %s.", agent_name)
            return

        self.fleet.register(agent_name, instance_name,discovery)

        self.transport.subscribe_instance(instance_name)

        self.logger.debug("Confirmed: agent=%s kind=%s instance=%s", agent_name, discovery.kind, instance_name)

    def _on_instance_released(self, agent_name: str, instance_name: str) -> None:
        """Handles the 'released' state transition."""
        self.fleet.remove(agent_name)
        self._instantiate_initiators.pop(agent_name, None)
        self._pending_discovery.pop(agent_name, None)
        self.transport.unsubscribe_instance(instance_name)


    @handles(MessageType.MISSION)
    def _handle_mission_reply(self, instance_name: str, env: MissionEnvelope) -> None:

        # feedback protection
        if env.sender == self.name:
            return

        agent_name = self.fleet.agent_of(instance_name)
        if agent_name is None:
            self.logger.warning("mission reply from unmapped instance=%s", instance_name)
            return
        session = self._mission_sessions.get(env.mission_id)
        if session is None:
            self.logger.debug("reply for retired session mission=%s from %s",
                              env.mission_id, agent_name)
            return

        out = session.handle(env, agent_name)
        self._send_session_out(out)


    @handles(MessageType.TWIN_STATE)
    def _handle_twin_state(self, instance_name: str, msg : TwinStatePayload) -> None:
        if not isinstance(msg, TwinStatePayload ):
            self.logger.warning("Non TwinStatePayload received, ignoring.")
            return

        # Route via instance -> agent lookup
        agent_name = self.fleet.agent_of(instance_name)
        if agent_name:
            self.fleet.ingest_twin_state(agent_name, msg)
        else:
            self.logger.warning("TwinStatePayload but no instance lookup, ignoring.")

    @handles(MessageType.OBSTACLE)
    def _handle_obstacle(self, instance_name: str, msg : ObstacleObservation) -> None:
        agent_name = self.fleet.agent_of(instance_name)      # was self._instance_to_agent → AttributeError
        if not agent_name:
            self.logger.warning("Obstacles received from non registered agent %s", agent_name)
            return

        self.logger.debug("received obstacle detection from agent %s", agent_name)
        self.obstacles.ingest(agent_name, msg, self._sim_step)

    # ------------------------------------------------------------------
    # Infrastructure & Simulation Logic
    # ------------------------------------------------------------------

    def step(self) -> None:
        try:
            self._step()
        except Exception:
            self.logger.exception("step failed at sim_step=%d", self._sim_step)

    def _step(self) -> None:
        self._sim_step += 1
        self._drain_inbox()


        if self._sim_step % self.plan_period == 0 or self._sim_step == 1:
            self.logger.debug("Draining %d mission(s)", len(self.pending_missions))
            self.plan_and_auction()

        self._sweep_sessions()

        if self._sim_step % self.perception_period == 0:
            now = self._sim_step
            self.obstacles.prune(now)
            self.grid_map.update_perception(self.obstacles.observations())

    def _drain_inbox(self, budget: int = 512) -> None:
        for _ in range(budget):
            try:
                topic, source, msg = self.inbox.get_nowait()
            except queue.Empty:
                return
            handler = getattr(self, self._DISPATCH[topic])
            try:
                handler(msg) if source is None else handler(source, msg)
            except Exception:
                self.logger.exception("handler for %s failed", topic)

    def _sweep_sessions(self) -> None:
        for mid, session in list(self._mission_sessions.items()):
            # Tick the sessions
            self._send_session_out(session.tick())

            # Check the state
            if session.state is SessionState.DONE:
                session.mission.mission_status = MissionStatus.COMPLETE

            elif session.state is SessionState.FAILED:
                # Because of session we could retry with second winner and so on (later addition)
                session.mission.mission_status = MissionStatus.PENDING

            if session.retirable:
                self._mission_sessions.pop(mid, None)
                self.logger.debug("session mission=%s retired", mid)

    def plan_and_auction(self) -> None:
        committed = self.committed
        eligible = [m for m in self._missions.values()
                    if m.mission_status is MissionStatus.PENDING and m.mission_id not in self._mission_sessions]

        if not eligible:
            return

        for mission, hints in self.mission_planner.assign_and_plan(
                missions=eligible, ugv_list=self.fleet.ugvs):

            live = {a: h for a, h in hints.items()
                    if self.fleet.instance_of(a) and a not in committed}
            if not live:
                self.logger.debug("no free agent for mission=%s", mission.mission_id)
                continue

            session = self._mission_sessions.get(mission.mission_id)
            if session is None:
                session = MissionSession(
                    aggregate_name=self.name,
                    mission=mission,
                    hints=live,
                    get_winner_fn=self.mission_planner.get_winner,
                    timeout=self.bid_timeout,
                    clock=self.now,
                )
                self._mission_sessions[mission.mission_id] = session

            mission.mission_status = MissionStatus.BIDDING
            self._send_session_out(session.open(hints=live))


    def get_winner(self, mission, available, hints) -> str | None:
        winner = self.mission_planner.get_winner(mission, available, hints)
        if winner is None:
            mission.mission_status = MissionStatus.PENDING
            self.logger.debug("auction failed mission=%s", mission.mission_id)
            return None

        return winner


    def detect_perception_faults(self, observations) -> None:
        """Need to reevaluate"""
        pass


    def trigger_global_reassignment(self) -> None:
        """Need to be reevaluated"""
        pass


    def _send_session_out(self, out: dict[str, MissionEnvelope]) -> None:
        """Sessions return {agent_name: envelope}; this is the only place that sends."""
        for agent_name, env in out.items():
            instance_name = self.fleet.instance_of(agent_name)
            if instance_name is None:
                self.logger.warning("no live instance for agent=%s, dropping %s",
                                    agent_name, env.handshake_status.value)
                continue
            self.transport.publish_to_instance(instance_name, MessageType.MISSION, env)

    def now(self) -> float:
        return self._sim_step * self.dt


    @property
    def committed(self) -> dict[str | None, str]:
        """Derived from the sessions, agents commited without actual ack"""
        return {s.committed_agent: mid
                for mid, s in self._mission_sessions.items() if s.committed_agent}


    @property
    def missions(self) -> list[Mission]:
        return list(self._missions.values())

    def add_mission(self, mission:Mission) -> None:
        mid = mission.mission_id
        if mid not in self._missions:
            self._missions[mid] = mission
        else:
            self.logger.warning("Adding mission that is already present: %s", mission.mission_id)

    @property
    def active_missions(self)-> list[Mission]:
        return [m for m in self._missions.values() if m.mission_status == MissionStatus.ACTIVE]

    @property
    def missions_in_flight(self) -> list[Mission]:
        """in flight missions are seen are missions with bidding open"""
        return [m for m in self._missions.values() if m.mission_status == MissionStatus.BIDDING]

    @property
    def pending_missions(self) -> list[Mission]:
        return [m for m in self._missions.values() if m.mission_status == MissionStatus.PENDING]