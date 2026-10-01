"""
Operator side of the layered discovery.

Each entry of `twin.discoveries_gui` holds the layers check_discovery() collected,
highest precedence first:

    reported   what the agent said about itself (always present)
    agent      config written for this agent by name
    type       default for its agent_type
    kind       default for its kind (ugv, uav)

build_form() turns the layers into one question per field; apply_resolution() turns
the answers back into AgentDiscoveryMessage fields. Only a custom answer's value
comes from the browser: every other value is looked up again here, so a client
cannot submit a value it was not offered.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any

from omegaconf import OmegaConf

from core_msgs.global_msgs.global_payloads import AgentDiscoveryMessage

from aggregate_twin.gui.discovery_validation import (
    DiscoveryValidationError, Rule, rules_for, validate,
)

PRECEDENCE = ("reported", "agent", "type", "kind")
CUSTOM = "custom"
MISSING = "???"

#: Who the agent is rather than how it is configured: carried over from the report,
#: never asked about.
IDENTITY_FIELDS = frozenset({"name", "agent_name", "kind", "agent_type", "namespace", "timestamp", "agent_id"})

_ANNOTATION_TYPES = {
    "bool": "boolean", "int": "number", "float": "number", "str": "string",
    "dict": "object", "Dict": "object", "Mapping": "object",
    "list": "list", "List": "list", "tuple": "list", "Tuple": "list", "Sequence": "list",
}


@dataclass(frozen=True)
class Candidate:
    source: str
    value: Any


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    candidates: tuple[Candidate, ...]
    rules: tuple[Rule, ...]

    @property
    def default_source(self) -> str | None:
        return self.candidates[0].source if self.candidates else None

    @property
    def conflicting(self) -> bool:
        return len({_fingerprint(c.value) for c in self.candidates}) > 1

    def candidate(self, source: str) -> Candidate | None:
        return next((c for c in self.candidates if c.source == source), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "selected": self.default_source,
            "conflict": self.conflicting,
            "candidates": [{"source": c.source, "value": c.value} for c in self.candidates],
            "rules": [rule.describe() for rule in self.rules],
        }


# ------------------------------------------------------------------ public
def build_form(agent_name: str, layers: dict) -> dict[str, Any]:
    return {
        "id": agent_name,
        "agent": _identity_summary(agent_name, layers),
        "sources": source_labels(agent_name, layers),
        "fields": [spec.to_json() for spec in field_specs(layers)],
    }


def summarize(agent_name: str, layers: dict) -> dict[str, Any]:
    specs = field_specs(layers)
    return {
        **_identity_summary(agent_name, layers),
        "fields": len(specs),
        "missing": sum(1 for s in specs if not s.candidates),
        "conflicts": sum(1 for s in specs if s.conflicting),
    }


def apply_resolution(layers: dict, choices: dict[str, dict]) -> dict[str, Any]:
    """Final AgentDiscoveryMessage kwargs. Raises DiscoveryValidationError with one
    message per failing field."""
    final = identity_fields(layers)
    errors: dict[str, str] = {}
    for spec in field_specs(layers):
        value, error = _answer(spec, choices.get(spec.name) or {})
        error = error or validate(value, spec.rules)
        if error:
            errors[spec.name] = error
        else:
            final[spec.name] = value
    if errors:
        raise DiscoveryValidationError(errors)
    return final


def field_specs(layers: dict) -> list[FieldSpec]:
    specs = []
    for definition in fields(AgentDiscoveryMessage):
        if definition.name in IDENTITY_FIELDS:
            continue
        candidates = tuple(_candidates(layers, definition.name))
        value_type = _declared_type(definition.type) or (
            _inferred_type(candidates[0].value) if candidates else "json")
        specs.append(FieldSpec(definition.name, value_type, candidates,
                               rules_for(definition.name, value_type)))
    return specs


def identity_fields(layers: dict) -> dict[str, Any]:
    names = {f.name for f in fields(AgentDiscoveryMessage)} & IDENTITY_FIELDS
    reported = layers.get("reported")
    out = {}
    for name in names:
        found, value = _lookup(reported, name, plain=False)
        if found:
            out[name] = value
    return out


def source_labels(agent_name: str, layers: dict) -> dict[str, dict[str, str]]:
    who = _identity_summary(agent_name, layers)
    kind, agent_type = who["kind"] or "kind", who["agent_type"] or "type"
    return {
        "reported": {"label": "Reported", "hint": f"What {agent_name} reported about itself"},
        "agent": {"label": "Agent config", "hint": f"Config written for {agent_name}"},
        "type": {"label": f"{agent_type} default", "hint": f"Default for every {agent_type}"},
        "kind": {"label": f"{kind} default", "hint": f"Default for every {kind}"},
        CUSTOM: {"label": "Custom", "hint": "Write your own value"},
    }


# ---------------------------------------------------------------- internals
def _candidates(layers: dict, name: str):
    found_values = []
    for source in PRECEDENCE:
        found, value = _lookup(layers.get(source), name)
        if found:
            found_values.append((source, value))
    for i, (source, value) in enumerate(found_values):
        if isinstance(value, dict):
            below = [v for _, v in found_values[i + 1:] if isinstance(v, dict)]
            if below:
                value = OmegaConf.to_container(OmegaConf.merge(*reversed(below), value))
        yield Candidate(source, value)

def _answer(spec: FieldSpec, choice: dict) -> tuple[Any, str | None]:
    source = choice.get("source") or spec.default_source
    if source is None:
        return None, None
    if source == CUSTOM:
        return choice.get("value"), None
    candidate = spec.candidate(source)
    if candidate is None:
        return None, "That source has no value for this field. Choose another."
    return candidate.value, None


def _identity_summary(agent_name: str, layers: dict) -> dict[str, Any]:
    reported = layers.get("reported")
    _, kind = _lookup(reported, "kind")
    _, agent_type = _lookup(reported, "agent_type")
    return {"id": agent_name, "kind": _text(kind), "agent_type": _text(agent_type)}


def _lookup(layer: Any, name: str, plain: bool = True) -> tuple[bool, Any]:
    """(found, value) for one field of one layer. Missing ("???") and None are absent."""
    if layer is None:
        return False, None
    if OmegaConf.is_config(layer):
        # `in` is False for "???" on a DictConfig, and reading one would raise.
        if name not in layer or OmegaConf.is_missing(layer, name):
            return False, None
        value = layer[name]
        if OmegaConf.is_config(value):
            value = OmegaConf.to_container(value, resolve=True)
    elif isinstance(layer, dict):
        value = layer.get(name)
    else:
        value = getattr(layer, name, None)
    if value is None or value == MISSING:
        return False, None
    return True, _plain(value) if plain else value


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and not isinstance(value, (str, bytes)):
        return _plain(tolist())
    return value


def _text(value: Any) -> str | None:
    value = _plain(value)
    return None if value in (None, MISSING) else str(value)


def _fingerprint(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _declared_type(annotation: Any) -> str | None:
    """Value type from a dataclass annotation, when it names exactly one kind of value."""
    if isinstance(annotation, type):
        text = annotation.__name__
    else:
        text = annotation if isinstance(annotation, str) else repr(annotation)
    text = text.replace("typing.", "").replace(" ", "")
    for wrapper in ("Optional", "Union"):
        if text.startswith(f"{wrapper}[") and text.endswith("]"):
            text = "|".join(_split_top_level(text[len(wrapper) + 1:-1], ","))
    members = [m for m in _split_top_level(text, "|") if m not in ("None", "NoneType")]
    if len(members) != 1:
        return None
    head = re.match(r"\w+", members[0])
    return _ANNOTATION_TYPES.get(head.group(0)) if head else None


def _split_top_level(text: str, separator: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, char in enumerate(text):
        depth += (char == "[") - (char == "]")
        if char == separator and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p for p in parts if p]


def _inferred_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, str):
        return "string"
    return "json"
