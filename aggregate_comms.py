"""
twin_network_node.py

Everything that knows flexNode exists. That's the whole point of the file.
"""
from __future__ import annotations

import jsonpickle

from flexCommunicator.clientLibraries.flcpy.flexNode import flexNode
from core_msgs.topic_contract import (
    MessageType, register_node_topics, load_topic_config, get_data_name,
)

EMPTY_COMM_MATRIX_PATH = "config/emptyCommMatrix.yaml"
INSTANCE_SPECIFIC_CONFIG = "config/specific_topic_config.yaml"

class AggregateNetworkNode(flexNode):

    def __init__(self,
                 flex_config,
                 global_topic_dict,
                 twin,
                 namespace,
                 node_name,
                 loop_freq: int
    ):

        super().__init__(
            config=flex_config["Aggregate_Twin_Config"],
            loaded_config=True,
            communicationMatrix=EMPTY_COMM_MATRIX_PATH,
            verbose=True,
        )
        self.twin = twin
        self.namespace = namespace
        self.node_name = node_name
        self.decode_errors = 0

        self._instance_topic_dict = load_topic_config(INSTANCE_SPECIFIC_CONFIG)
        self._instance_topics: dict[str, object] = {}

        self._published = register_node_topics(
            node=self, topic_dict=global_topic_dict,
            namespace=namespace, agent_id=node_name,
            in_callbacks={
                MessageType.DISCOVERY:   lambda p: self._ingest(MessageType.DISCOVERY, p),
                MessageType.INSTANTIATE: lambda p: self._ingest(MessageType.INSTANTIATE, p),
            },
        )

        self.timer = self.create_timer(timer_period=1 / loop_freq,
                                       callback=twin.step, autostart=True)

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