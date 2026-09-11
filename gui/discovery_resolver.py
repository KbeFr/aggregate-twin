"""
discovery_resolver.py
----------------------
Turns one entry of AggregateTwin.discoveries_gui into the field-by-field
options the console's Discovery panel renders, and turns the operator's
choices back into a plain field dict ready for DiscoveryMessage(**fields).

This assumes check_discovery()'s GUI branch has been patched to store
*labeled* layers instead of a bare list -- see the aggregate_twin.py patch
notes. Concretely, each entry of discoveries_gui is expected to look like:

    {
        "reported": <DictConfig>,          # always present
        "agent":    <DictConfig or absent>,  # agent_specific override, if any
        "type":     <DictConfig or absent>,  # agent_type default, if any
        "kind":     <DictConfig or absent>,  # kind default, if any
    }

Without that patch, discoveries_gui holds an unlabeled list and there is no
reliable way to tell which entry came from which layer (the list only
contains whichever layers exist for that particular agent, so the position
of "agent_specific" vs "type" vs "kind" shifts agent to agent) -- so this
module can't be used against the unpatched version.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Optional

from core_msgs.global_msgs.global_payloads import DiscoveryMessage

try:
    from omegaconf import OmegaConf
except ImportError:  # pragma: no cover - only exercised outside the real env
    OmegaConf = None

PRECEDENCE = ("reported", "agent", "type", "kind")
SKIP_FIELDS = {"timestamp", "agent_name" , "kind" , "agent_type" , "namespace"}

def _to_plain(value: Any) -> Any:
    if OmegaConf is not None and OmegaConf.is_config(value):
        return OmegaConf.to_container(value, resolve=True)
    return value


def _infer_type(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _build_labels(reported: dict[str, Any]) -> dict[str, str]:
    """Build specific source labels based on the reported agent identities."""
    name = reported.get("agent_name", "unknown")
    atype = reported.get("agent_type") or "generic"
    kind = getattr(reported.get("kind"), "value", reported.get("kind")) or "unknown"

    return {
        "reported": f"Reported ({name})",
        "agent": f"Override: {name}",
        "type": f"Default for type '{atype}'",
        "kind": f"Default for kind '{kind}'",
    }


def resolve_discovery_fields(layers: dict[str, dict]) -> dict[str, dict]:
    """Build candidate options for all valid DiscoveryMessage fields across config layers."""
    labels = _build_labels(layers.get("reported") or {})

    # Discover candidate field names directly from the dataclass schema
    target_fields = sorted(
        f.name for f in fields(DiscoveryMessage) if f.name not in SKIP_FIELDS
    )

    resolved: dict[str, dict] = {}
    for name in target_fields:
        candidates = [
            {
                "source": src,
                "label": labels.get(src, src),
                "value": _to_plain(layers[src][name]),
            }
            for src in PRECEDENCE
            if src in layers and name in layers[src] and layers[src][name] != "???"
        ]

        if candidates:
            resolved[name] = {
                "type": _infer_type(candidates[0]["value"]),
                "selected": candidates[0]["source"],
                "candidates": candidates,
            }

    return resolved

def apply_resolution(layers: dict, choices: dict) -> dict[str, Any]:
    """Turn the operator's submitted choices into a final {field: value} dict.

    `choices`: {field: {"source": "reported"|"agent"|"type"|"kind"}}
                       or {"source": "custom", "value": <parsed value>}

    Re-derives the value for every non-custom source from `layers` itself
    rather than trusting a value the client might send back for those --
    the client only gets to inject a value when it explicitly chose
    'custom'. Everything else is looked up here, server-side.
    """
    resolved = resolve_discovery_fields(layers)
    final: dict[str, Any] = {}
    for name, spec in resolved.items():
        choice = choices.get(name) or {"source": spec["selected"]}
        source = choice.get("source", spec["selected"])
        if source == "custom":
            final[name] = choice.get("value")
            continue
        match = next((c for c in spec["candidates"] if c["source"] == source), None)
        if match is None:
            raise ValueError(f"field '{name}': no '{source}' candidate is available for this agent")
        final[name] = match["value"]
    return final
