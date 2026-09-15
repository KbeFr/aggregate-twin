import logging
import os
import time




# --- MONKEY PATCH FOR MQTT SUBSCRIBING PROBLEM ---
import paho.mqtt.client as mqtt

# Save the original subscribe method
_original_subscribe = mqtt.Client.subscribe

# Create a safe wrapper that catches and discards unexpected kwargs (like 'isService')
def _safe_subscribe(self, topic, qos=0, options=None, properties=None, **kwargs):
    return _original_subscribe(self, topic, qos=qos, options=options, properties=properties)

# Override the library method globally
mqtt.Client.subscribe = _safe_subscribe

# -----------------------------

from comms.aggregate_comms import AggregateNetworkNode
from gui.aggregate_gui import start_gui
from core.aggregate_twin import AggregateTwin
from core_msgs.topic_contract import load_topic_config
from core_msgs.utils.utils import load_config, format_nested_strings
from utils.mission_logger import MissionLogger



## Environment variables from Dockerfile / docker-compose
TICK_HZ = float(os.environ.get("TWIN_TICK_HZ", "10"))
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
NAMESPACE = os.environ.get("TWIN_NAMESPACE", "default_ns")
TWIN_NAME = os.environ.get("TWIN_NAME", "AggregateTwin")
PERCEPTION_SOURCE = os.environ.get("PERCEPTION_SOURCE", "static")  # static | sim | aruco | merged
WORLD_CONFIG_PATH = os.environ.get("WORLD_CONFIG_PATH", "config/empty_world.yaml")
TWIN_GUI_PORT = os.environ.get("TWIN_GUI_PORT", "8082")

logger = logging.getLogger(__name__)


def main() -> None:

    flex_config_dict = load_config("config/config.yaml")
    # change mqtt and redis addresses
    formatted_flex_config = format_nested_strings(
        flex_config_dict,
        mqtt_address=MQTT_BROKER_HOST,
        redis_address=REDIS_HOST,
        node_id=TWIN_NAME
    )

    global_topic_dict = load_topic_config("config/global_topic_config.yaml")
    world_raw = load_config(WORLD_CONFIG_PATH)


    mission_logger = MissionLogger()

    twin = AggregateTwin(
        world=world_raw,
        mission_logger=mission_logger,
        namespace=NAMESPACE,
        name=TWIN_NAME,
        loop_freq=int(TICK_HZ),
    )

    network_node = AggregateNetworkNode(
        flex_config=formatted_flex_config,
        global_topic_dict=global_topic_dict,
        twin=twin,
        namespace=NAMESPACE,
        loop_freq=int(TICK_HZ),
        gui_port=TWIN_GUI_PORT,
    )

    network_node.spin()
    twin.setup_transport(network_node)


    start_gui(twin, port=int(TWIN_GUI_PORT))

    try:
        logger.info("Started Aggregate Twin.")
        while True:
            time.sleep(1)  # Sleep to avoid busy-waiting
    except KeyboardInterrupt:
        network_node.shutdown()
        logger.warning("\nKeyboard interruption detected. Exiting...")

if __name__ == "__main__":
    main()
