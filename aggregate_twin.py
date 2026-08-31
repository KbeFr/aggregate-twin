"""
aggregate_twin.py

Orchestrates multiple agents via instance twins using a decoupled
Initiator pattern for lifecycle and mission handshakes.
"""
from __future__ import annotations

import numpy as np
import concurrent.futures
import jsonpickle

from flexCommunicator.clientLibraries.flcpy.flexNode import flexNode

from core_msgs.agents_contract import parse_agent_kind
from core_msgs.instance_aggregate.handshake_shared import HandshakeStatus
from core_msgs.topic_contract import MessageType, register_node_topics, load_topic_config, get_data_name
from core_msgs.global_msgs.global_payloads import DiscoveryMessage
from core_msgs.instance_aggregate.payloads import ObstacleObservation, TwinStatePayload
from core_msgs.instance_aggregate.instantiate_handshake import  InstantiateInitiator
from core_msgs.instance_aggregate.mission_handshake import MissionInitiator, MissionAuction, \
    InitiatorState, MissionEnvelope
from core_msgs.instance_aggregate.mission import Mission, MissionStatus
from functions.a_star_custom import AStarPlannerCustom
from obstacle_registry import ObstacleRegistry

from world_handler import WorldConfig
from fleet_registry import FleetRegistry
from functions.grid_map import GlobalGridMap
from functions.mission_planner import MissionPlanner


class OverArchingTwin(flexNode):
    def __init__(
        self,
        flex_config: dict,
        global_topic_dict: dict,
        world: dict,
        mission_logger,
        namespace: str = "default_ns",
        name: str = "AggregateTwin",
        loop_freq: int = 10
    ) -> None:

        super().__init__(
            config=flex_config["Aggregate_Twin_Config"],
            loaded_config=True,
            communicationMatrix="config/emptyCommMatrix.yaml",
            verbose=True
        )
        self.name = name
        self.namespace = namespace

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
        self._seen_running: set[str] = set()         # mission in twin_state

        self.bid_timeout = 100

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

        self._planner_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._planning_future = None
        self._is_planning_active = False
        self._active_paths: dict[str, np.ndarray] = {}

        # --- Protocol & Topology State ---
        self._instantiate_initiators: dict[str, InstantiateInitiator] = {}
        self._mission_initiators: dict[str, MissionInitiator] = {}


        self._pending_discovery: dict[str, DiscoveryMessage] = {}
        self._instance_obstacles: dict[str, ObstacleObservation] = {}


        self._mission_auctions: dict[str, MissionAuction] = {}
        # For race conditions
        self._committed: dict[str, str] = {}      # agent_name -> mission_id

        self._instance_topics: dict[str, dict] = {}
        self._instance_topic_dict = load_topic_config("config/specific_topic_config.yaml")

        # Global Topic Registration
        self._published = register_node_topics(
            node=self, topic_dict=global_topic_dict, namespace=namespace, agent_id=name,
            in_callbacks={
                MessageType.DISCOVERY: self._handle_discovery,
                MessageType.INSTANTIATE: self._handle_instantiate_reply,
            },
        )

        self.logger.debug("Init complete. namespace=%s resolution=%.3f loop_freq=%d",
             self.namespace, effective_resolution, loop_freq)

        self.timer = self.create_timer(timer_period=self.dt, callback=self.step, autostart=True)


    # ------------------------------------------------------------------
    # Dispatch & Teardown Handlers
    # ------------------------------------------------------------------

    def _request_instance(self, agent_name: str, discovery_msg: DiscoveryMessage) -> None:
        if agent_name in self._instantiate_initiators:
            self.logger.debug("Instantiating handshake already registered for agent=%s", agent_name)
            return

        self.logger.debug("Requesting instance for agent=%s", agent_name)
        initiator = InstantiateInitiator(agent_name, self.name)
        self._instantiate_initiators[agent_name] = initiator

        self.set_data(MessageType.INSTANTIATE.value, initiator.request(discovery_msg))

    def _release_instance(self, agent_name: str) -> None:
        initiator = self._instantiate_initiators.get(agent_name)
        if not initiator or not initiator.confirmed:
            self.logger.warning("Cannot release %s: No live instance.", agent_name)
            return

        self.logger.debug("Releasing instance for agent=%s", agent_name)
        self.set_data(MessageType.INSTANTIATE.value, initiator.cancel())

    def _release_commited_agent(self, agent_name: str | None = None, mission_id: str | None = None) -> None:
        for agent, mid in list(self._committed.items()):
            if agent == agent_name or mid == mission_id:
                self._committed.pop(agent, None)


    def _cancel_mission_on_agent(self, mission_id: str, agent_name: str) -> None:
        instance_name = self.fleet.instance_of(agent_name)
        if not instance_name:
            self.logger.warning("Cannot cancel mission on %s: No live instance.", agent_name)
            return

        initiator = self._mission_initiators.get(mission_id)
        if not initiator:
            return

        data_name = get_data_name(instance_name, MessageType.MISSION)
        self.set_data(data_name, initiator.cancel())
        self.logger.debug("Cancelling mission=%s on agent=%s", mission_id, agent_name)


    # ------------------------------------------------------------------
    # Message Ingestion Callbacks
    # ------------------------------------------------------------------

    def _handle_discovery(self, payload: str) -> None:
        msg = jsonpickle.decode(payload)

        agent_name = msg.robot_name
        if not agent_name or self.fleet.instance_of(agent_name) or agent_name in self._pending_discovery:
            self.logger.debug("Ignoring discovery for agent=%s (already live or pending)", agent_name)
            return

        self.logger.debug("Discovery received: agent=%s", agent_name)
        self._pending_discovery[agent_name] = msg
        self._request_instance(agent_name, msg)

    def _handle_instantiate_reply(self, payload: str) -> None:
        """ Parse and Route."""
        env = jsonpickle.decode(payload)

        # feedback message protection (inout)
        if env.handshake_status == HandshakeStatus.REQUEST:
            return
        agent_name, instance_name = env.agent_name, env.instance_name

        # Basic boundary validation
        if not instance_name:
            self.logger.error("Instance name missing in reply payload for agent=%s.", agent_name)
            return

        initiator = self._instantiate_initiators.get(agent_name)
        if not initiator:
            return  # Or log a debug message

        # Let the initiator resolve the protocol rules
        result_action = initiator.handle(env)

        # Dispatch to dedicated handlers
        if result_action == "confirmed":
            self._on_instance_confirmed(agent_name, instance_name)
        elif result_action == "released":
            self._on_instance_released(agent_name, instance_name)
        elif result_action in ("rejected", "release_rejected"):
            self._release_commited_agent(env.agent_name)
            self.logger.warning("Instance %s for agent=%s", result_action, agent_name)
        else:
            self.logger.error("Unknown initiator action: %s", result_action)

    def _on_instance_confirmed(self, agent_name: str, instance_name: str) -> None:
        """Handles the 'confirmed' state transition."""
        if self.fleet.agent_of(instance_name):
            self.logger.warning("Agent=%s confirmed again while already live, ignoring.", agent_name)
            return

        msg = self._pending_discovery.pop(agent_name, None)
        if not msg:
            self.logger.error("No Discovery message found for agent %s.", agent_name)
            return
        if not msg.kind:
            self.logger.error("No AgentKind in discovery payload, cannot register %s.", agent_name)
            return

        radius = msg.radius if msg.radius else None
        if radius is None:
            self.logger.error("No AgentRadius in discovery payload, using default radius %s.", agent_name)

        # Execute state changes
        self.fleet.register(agent_name, parse_agent_kind(msg.kind), instance_name, radius)
        self._subscribe_instance(instance_name)

        self.logger.debug("Confirmed: agent=%s kind=%s instance=%s", agent_name, msg.kind, instance_name)

    def _on_instance_released(self, agent_name: str, instance_name: str) -> None:
        """Handles the 'released' state transition."""
        self._instance_topics.pop(instance_name, None)
        self.fleet.remove(agent_name)
        self.logger.debug("Released: agent=%s", agent_name)

    def _handle_mission_reply(self, instance_name: str, payload: str) -> None:
        env = jsonpickle.decode(payload)

        if env.sender == self.name:
            return

        mission_id = env.mission_id
        agent_name = self.fleet.agent_of(instance_name)

        # Now there are two kinds of possible mission message there can be received here.
        # Bidding happens through the missionAuctions
        # Directed ACK ->  for 3 way handshake (needed if multiple Auctions possible)

        # First filter the bidders ig (not good cause handshake status bleed)
        auction = self._mission_auctions.get(mission_id)
        if auction and agent_name in auction.hints:
            auction.handle(env, agent_name)
            return

        # catch for late bids that have not auction (now just log catch)
        if env.handshake_status == HandshakeStatus.BID:
            self.logger.warning("Received bid while no auction active for agent=%s", agent_name)

        # Handle the rest like before
        initiator = self._mission_initiators.get(mission_id)
        if not initiator:
            return

        result_action = initiator.handle(env)

        mission = self._missions.get(mission_id)

        if result_action is InitiatorState.CONFIRMED and mission:
            mission.assigned_ugv = agent_name
            mission.mission_status = MissionStatus.ACTIVE

        elif result_action is InitiatorState.REJECTED:
            self._mission_initiators.pop(mission_id, None)
            self._release_commited_agent(mission_id=mission_id)
            if mission:
                mission.mission_status = MissionStatus.PENDING

        elif result_action is InitiatorState.IDLE:              # CANCEL_ACK
            self._mission_initiators.pop(mission_id, None)
            self._release_commited_agent(mission_id=mission_id)
            if mission and mission.mission_status is MissionStatus.ACTIVE:
                mission.mission_status = MissionStatus.PENDING

    def _handle_twin_state(self, instance_name: str, payload: str) -> None:
        msg = jsonpickle.decode(payload)
        if not isinstance(msg, TwinStatePayload ):
            self.logger.warning("Non TwinStatePayload received, ignoring.")

        # Route via instance -> agent lookup
        agent_name = self.fleet.agent_of(instance_name)
        if agent_name:
            self.fleet.ingest_twin_state(agent_name, msg)
        else:
            self.logger.warning("TwinStatePayload but no instance lookup, ignoring.")


    def _handle_obstacle(self, instance_name: str, payload: str) -> None:
        agent_name = self.fleet.agent_of(instance_name)      # was self._instance_to_agent → AttributeError
        if not agent_name:
            return                                                     # TODO check
        self.obstacles.ingest(agent_name, jsonpickle.decode(payload), self._sim_step)

    # ------------------------------------------------------------------
    # Infrastructure & Simulation Logic
    # ------------------------------------------------------------------

    def _subscribe_instance(self, instance_name: str) -> None:
        self._instance_topics[instance_name] = register_node_topics(
            node=self, topic_dict=self._instance_topic_dict,
            namespace=self.namespace, agent_id=instance_name,
            in_callbacks={
                MessageType.MISSION: lambda p, aid=instance_name: self._handle_mission_reply(aid, p),
                MessageType.TWIN_STATE: lambda p, aid=instance_name: self._handle_twin_state(aid, p),
                MessageType.OBSTACLE: lambda p, aid=instance_name: self._handle_obstacle(aid, p),
            },
        )
        self.logger.debug("Subscribed to instance=%s topics", instance_name)

    def step(self) -> None:
        self._sim_step += 1

        if self._sim_step % self.plan_period == 0 or self._sim_step == 1:
            # borrow the period
            self._sweep_active_missions()
            self._tick_initiators()

            self.logger.debug("Draining %d mission(s)", len(self.pending_missions))
            self.plan_and_auction()


        if self._is_planning_active and self._planning_future and self._planning_future.done():
            self.logger.debug("Async replanning finished, applying new paths")
            self._is_planning_active, self._planning_future = False, None
        # tick all auctions
        for a in list(self._mission_auctions.values()): a.tick()

        if self._sim_step % self.perception_period == 0:
            now = self._sim_step
            self.obstacles.prune(now)
            self.grid_map.update_perception(self.obstacles.observations())


    def _tick_initiators(self) -> None:
        """Check the initiators, if the awarding is expired -> release"""
        for mid, ini in list(self._mission_initiators.items()):
            if not ini.expired:
                continue
            self.logger.warning("award timed out mission=%s agent=%s", mid, ini.agent_name)
            self._mission_initiators.pop(mid, None)
            self._release_commited_agent(agent_name=ini.agent_name)
            mission = self._missions.get(mid)
            if mission and mission.mission_status is MissionStatus.BIDDING:
                mission.mission_status = MissionStatus.PENDING

    def _sweep_active_missions(self) -> None:
        """
        An ACTIVE mission whose agent stopped reporting it is done.
        TODO : Maybe handle this in handle_twin_state? or let instance report this
        """
        for mid, mission in list(self._missions.items()):
            if mission.mission_status is not MissionStatus.ACTIVE or not mission.assigned_ugv:
                continue
            entry = self.fleet.get(mission.assigned_ugv)
            if entry is None:                       # agent vanished → re-plan
                mission.mission_status = MissionStatus.PENDING
                mission.assigned_ugv = None
                self._release_commited_agent(mission_id=mid)
                continue
            if entry.mission_id == mid:
                self._seen_running.add(mid)         # telemetry caught up
            elif mid in self._seen_running:         # it had it, now it doesn't
                mission.mission_status = MissionStatus.COMPLETE
                self._release_commited_agent(mission_id=mid)
                self._mission_initiators.pop(mid, None)
                self.logger.debug("mission=%s COMPLETE on %s", mid, mission.assigned_ugv)


    def plan_and_auction(self):
        # Get hints for the best agents
        mission_list = self.mission_planner.assign_and_plan(missions=self.missions, ugv_list=self.fleet.ugvs)

        for mission, hints in mission_list:
            # Check if instance exist for planned agent (should always be)
            live = {a: h for a, h in hints.items() if self.fleet.instance_of(a)}
            if not live:
                self.logger.warning("No live instance for mission=%s", mission.mission_id)
                continue
            auction = MissionAuction(aggregate_name=self.name,
                                     mission=mission,
                                     hints=hints,
                                     on_complete=self._on_bids,
                                     timeout=self.bid_timeout)
            self._mission_auctions[mission.mission_id] = auction
            mission.mission_status = MissionStatus.BIDDING # for planner exclusion

            for agent_name, env in auction.open().items():
                self._send_to_agent(agent_name, env)  # see below


    def _on_bids(self, mission, bids, hints) -> None:
        self._mission_auctions.pop(mission.mission_id, None)      # erase

        # An agent already awarded another mission is out, even if its telemetry
        # hasn't caught up yet.
        available = {
            name: bid for name, bid in bids.items()
            if self._committed.get(name, mission.mission_id) == mission.mission_id
        }


        winner = self.mission_planner.get_winner(mission, available, hints)
        if winner is None:
            mission.mission_status = MissionStatus.PENDING
            self.logger.debug("auction failed mission=%s", mission.mission_id)
            return

        self._committed[winner] = mission.mission_id

        ini = MissionInitiator(mission.mission_id, winner, self.name)
        self._mission_initiators[mission.mission_id] = ini
        self._send_to_agent(winner, ini.award(mission))

        for loser, bid in bids.items():
            if loser != winner and bid is not None:
                self._send_to_agent(loser, MissionEnvelope(
                    mission_id=mission.mission_id,
                    handshake_status=HandshakeStatus.CANCEL,
                    sender=self.name,
                ))

    def _send_to_agent(self, agent_name: str, env: MissionEnvelope) -> None:
        instance_name = self.fleet.instance_of(agent_name)
        if not instance_name:
            return
        env.sender = self.name
        data_name = get_data_name(instance_name, MessageType.MISSION)
        self.set_data(data_name, env)
        self.logger.debug("Send data for agent %s data to data name: %s", agent_name, data_name)


    def detect_perception_faults(self, observations) -> None:
        """Need to reevaluate"""
        pass


    def trigger_global_reassignment(self) -> None:
        """Need to be reevaluated"""
        pass

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
