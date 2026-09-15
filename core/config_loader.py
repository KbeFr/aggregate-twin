import logging
from dataclasses import fields
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from core_msgs.global_msgs.global_payloads import DiscoveryMessage
from core_msgs.utils.utils import load_config

logger = logging.getLogger(__name__)

def sanitize(value):
    """Recursively swap numpy scalars/arrays for native Python types"""
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and not isinstance(value, (str, bytes)):
        try:
            return sanitize(tolist())
        except Exception:
            pass
    return value


def structured(obj):
    """ omegaconf only treats "???" as not filled in for some reason, so convert None -> "???" """
    import copy
    obj = copy.copy(obj)  # don't mutate the caller's live DiscoveryMessage
    for field in fields(obj.__class__):
        value = getattr(obj, field.name)
        setattr(obj, field.name, "???" if value is None else sanitize(value))
    return OmegaConf.structured(obj)

def check_discovery( msg : DiscoveryMessage, configs : dict , autocomplete : bool ) -> DiscoveryMessage | dict:
    layers: dict[str, Any] = {"reported": structured(msg)}
    agent_kind = msg.kind.value  # ugv, uav
    # layer 1 -> on agent_name level
    agent_specific = configs.get("agent", {}).get(msg.agent_name)
    if agent_specific is not None:
        layers["agent"] = OmegaConf.create(agent_specific)
    # layer 2 -> on agent_type level (based on kind)
    type_specific = configs.get(agent_kind, {}).get(msg.agent_type)
    if type_specific is not None:
        layers["type"] = OmegaConf.create(type_specific)
    # layer 3 -> default kind configs
    default_kind = configs.get(agent_kind, {}).get("default")
    if default_kind is not None:
        layers["kind"] = OmegaConf.create(default_kind)
    if autocomplete:
        # lowest priority first, so later entries win the merge:
        # kind default < type default < agent-specific < self-reported
        ordered = [layers[k] for k in ("kind", "type", "agent", "reported") if k in layers]
        disc = OmegaConf.merge(*ordered)
        # can check the missing still, but will just log and not act on it for now
        container = OmegaConf.to_container(disc, throw_on_missing=False)
        missing = [k for k, v in container.items() if v == "???"]
        if missing:
            logger.warning(
                "agent=%s: no config layer (or self-report) could fill %s, leaving as None",
                msg.agent_name, ", ".join(missing))
            for k in missing:
                container[k] = None
        return DiscoveryMessage(**container)
    else:
        # here the gui will first ask human intervention and validation of discovery
        gui_dict = {msg.agent_name: layers}
        return gui_dict

def load_agent_configs( path : str | Path ) -> dict:
    agent_config = {}
    # go through all the folders in AGENT_CONFIG_PATH, then the files in them and store them
    #dict[folder_first_work][file_first_word] = config.yaml
    for item in Path(path).iterdir():
        if item.is_dir():
            agent_config[item.name.split("_")[0]] = load_agent_configs(item)
        elif item.is_file():
            agent_config[item.name.split("__")[0]] = load_config(item)
    return agent_config
