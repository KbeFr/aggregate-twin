from __future__ import annotations

import jsonpickle

from flexCommunicator.clientLibraries.flcpy.flexNode import flexNode
from core_msgs.topic_contract import (
    MessageType, register_node_topics, load_topic_config, get_data_name,
)
from flexCommunicator.clientLibraries.flcpy.utils.constants import APPLICATION_STATUS

from flexCommunicator.clientLibraries.flcpy.flexCloud.flexCloudVariable import flexCloudVariable

from flexCommunicator.clientLibraries.flcpy.flexCloud.flexCloudConfiguration import flexCloudConfigParameter

EMPTY_COMM_MATRIX_PATH = "config/emptyCommMatrix.yaml"
INSTANCE_SPECIFIC_CONFIG = "config/specific_topic_config.yaml"

class AggregateNetworkNode(flexNode):
    """FlexNode interface to aggregate twin """
    def __init__(self,
                 flex_config : dict,
                 global_topic_dict : dict,
                 twin,
                 namespace : str,
                 loop_freq: int,
                 gui_port: int,
    ):

        super().__init__(
            config=flex_config["Aggregate_Twin_Config"],
            loaded_config=True,
            communicationMatrix=EMPTY_COMM_MATRIX_PATH,
            verbose=True,
        )
        self.twin = twin
        self.decode_errors = 0

        self._instance_topic_dict = load_topic_config(INSTANCE_SPECIFIC_CONFIG)
        self._instance_topics: dict[str, object] = {}

        self._published = register_node_topics(
            node=self, topic_dict=global_topic_dict,
            namespace=namespace, agent_id=twin.name,
            in_callbacks={
                MessageType.DISCOVERY:   lambda p: self._ingest(MessageType.DISCOVERY, p),
                MessageType.INSTANTIATE: lambda p: self._ingest(MessageType.INSTANTIATE, p),
            },
        )

        self.timer = self.create_timer(timer_period=1 / loop_freq,
                                       callback=twin.step, autostart=True)

        self.application_status.set(value=APPLICATION_STATUS.RUNNING)

        self._gui_port = flexCloudVariable(name="GUI_PORT", initial_value=gui_port)
        self._node_name = flexCloudVariable(name="NODE_NAME", initial_value=twin.name)
        self._namespace = flexCloudVariable(name="NAMESPACE", initial_value=namespace)
        self._pending_instances = flexCloudVariable(name="PENDING_INSTANCES", initial_value=0)
        self._pending_agents = flexCloudVariable(name="PENDING_AGENTS", initial_value=0)
        self._paired_nodes = flexCloudVariable(name="PAIRED_NODES", initial_value=0)


        self.register_flexCloud_variable(self._gui_port)
        self.register_flexCloud_variable(self._node_name)
        self.register_flexCloud_variable(self._namespace)
        self.register_flexCloud_variable(self._pending_instances)
        self.register_flexCloud_variable(self.pending_agents)
        self.register_flexCloud_variable(self._paired_nodes)

        self.multiplier = flexCloudConfigParameter(name="multiplier",initial_value=1)
        self.register_flexCloud_variable(self.multiplier)


    # --- inbound: decode, then queue. Runs on transport threads. -------------

    def _ingest(self, kind: str, payload: str, source: str | None = None) -> None:
        try:
            msg = jsonpickle.decode(payload)
        except Exception:
            self.decode_errors += 1
            self.logger.exception("decode failed kind=%s source=%s", kind, source)
            return
        self.twin.inbox.put((kind, source, msg))

    # --- outbound -----------------------------------------------------------

    def publish_global(self, msg_type: MessageType, payload) -> None:
        self.set_data(msg_type.value, payload)

    def publish_to_instance(self, instance_name: str, msg_type: MessageType, payload) -> None:
        self.set_data(get_data_name(instance_name, msg_type), payload)

    # --- subscription lifecycle ---------------------------------------------

    def subscribe_instance(self, instance_name: str) -> None:
        self._instance_topics[instance_name] = register_node_topics(
            node=self, topic_dict=self._instance_topic_dict,
            namespace=self.namespace, agent_id=instance_name,
            in_callbacks={
                MessageType.MISSION:    lambda p, s=instance_name: self._ingest(MessageType.MISSION, p, s),
                MessageType.TWIN_STATE: lambda p, s=instance_name: self._ingest(MessageType.TWIN_STATE, p, s),
                MessageType.OBSTACLE:   lambda p, s=instance_name: self._ingest(MessageType.OBSTACLE, p, s),
            },
        )
        self.logger.debug("Subscribed to instance=%s topics", instance_name)


    def unsubscribe_instance(self, instance_name: str) -> None:
        """
        *Still needs the logic in the flex node to actually be able to unsubscribe
        Can filter out unsibscribed topics when they arrive or send here tho.
        """
        self._instance_topics.pop(instance_name, None)

    @property
    def namespace(self):
        return self._namespace.get()