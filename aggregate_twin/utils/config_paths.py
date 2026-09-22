from importlib.resources import files

DEFAULT_AGENT_CONFIG_PATH = str( files("aggregate_twin").joinpath("config", "default_agent_config.yaml"))

FLEX_CONFIG_PATH = str( files("aggregate_twin").joinpath("config", "config.yaml"))

GLOBAL_TOPIC_PATH = str( files("aggregate_twin").joinpath("config", "global_topic_config.yaml"))

INSTANCE_TOPIC_PATH = str( files("aggregate_twin").joinpath("config", "specific_topic_config.yaml"))

EMPTY_COMM_MATRIX_PATH = str( files("aggregate_twin").joinpath("config", "emptyCommMatrix.yaml"))

AGENT_CONFIG_FILES_PATH = str( files("aggregate_twin").joinpath("config", "agent_configs"))