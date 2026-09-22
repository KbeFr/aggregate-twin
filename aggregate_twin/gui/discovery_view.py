"""Discovery review: the queue every sheet carries, and one agent's form."""
from __future__ import annotations

from typing import Any

from aggregate_twin.gui.discovery_resolver import build_form, summarize
from aggregate_twin.gui.http_router import NotFound


def review_state(twin: Any) -> dict[str, Any]:
    return {
        "autocomplete": bool(getattr(twin, "autocomplete", False)),
        "queue": [summarize(name, layers) for name, layers in list(twin.discoveries_gui.items())],
    }


def discovery_form(twin: Any, agent_name: str) -> dict[str, Any]:
    return build_form(agent_name, pending_layers(twin, agent_name))


def pending_layers(twin: Any, agent_name: str) -> dict:
    layers = twin.discoveries_gui.get(agent_name)
    if layers is None:
        raise NotFound(f"{agent_name} is no longer waiting for review.")
    return layers
