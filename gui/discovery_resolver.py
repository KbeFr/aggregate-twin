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

from typing import Any, Optional

try:
    from omegaconf import OmegaConf
except ImportError:  # pragma: no cover - only exercised outside the real env
    OmegaConf = None

LABELS = {
    "reported": "Reported by agent",
    "agent": "agent-specific override",
    "type": "type default",
    "kind": "kind default",
}

# Highest priority first. Only affects which candidate is *pre-selected* --
# every candidate stays clickable in the GUI regardless of this order.
PRECEDENCE = ("reported", "agent", "type", "kind")

# DiscoveryMessage carries a timestamp that isn't something an operator
# reviews or resolves; drop it from the field list.
SKIP_FIELDS = {"timestamp"}


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


def resolve_discovery_fields(layers: dict) -> dict[str, dict]:
    """Build {field_name: {"type", "candidates", "selected"}} for one pending agent.

    A field is omitted if no layer has a real value for it (every layer is
    either absent or holds the OmegaConf missing marker "???") -- the
    operator has to use "write my own" in the GUI for those, there's nothing
    to pre-fill.
    """
    # Only ever resolve fields DiscoveryMessage actually has. The "reported"
    # layer is built straight from the dataclass (see AggregateTwin.structured),
    # so its key set *is* the field list -- the type/kind/agent yaml layers can
    # carry extra keys (e.g. 'battery', 'controller') that aren't part of
    # DiscoveryMessage at all, and those must never reach DiscoveryMessage(**fields).
    reported_layer = layers.get("reported") or {}
    field_names: set[str] = set(reported_layer.keys()) - SKIP_FIELDS

    resolved: dict[str, dict] = {}
    for name in sorted(field_names):
        candidates = []
        for source in PRECEDENCE:
            cfg = layers.get(source)
            if cfg is None or name not in cfg:
                continue
            raw = cfg[name]
            if raw == "???":          # OmegaConf's missing-value marker
                continue
            candidates.append({"source": source, "label": LABELS[source], "value": _to_plain(raw)})
        if candidates:
            resolved[name] = {
                "type": _infer_type(candidates[0]["value"]),
                "candidates": candidates,
                "selected": candidates[0]["source"],  # candidates is already in PRECEDENCE order
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
