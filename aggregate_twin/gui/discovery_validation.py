"""
Rules for the fields an operator completes in a discovery.

A rule is a small value object. `describe()` travels with the form so the browser
runs the same check while the operator types (static/js/discovery/validation.js
implements every rule by the same `name`); `error()` runs again here on submit, and
that is the check that counts. A rule the browser does not know is skipped there.

Adding a check:
    1. subclass Rule with a unique `name` and an `accepts()`
    2. implement the same name in validation.js (optional, for live feedback)
    3. attach it to a field in FIELD_SCHEMAS

Patterns run as JavaScript RegExp in the browser too, so keep them to the syntax
both engines share.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, ClassVar

TYPE_MESSAGES = {
    "string": "Expected text.",
    "number": "Expected a number.",
    "boolean": "Expected true or false.",
    "object": "Expected a JSON object, like {\"key\": 1}.",
    "list": "Expected a JSON list, like [1, 2].",
    "json": "Expected a JSON value.",
}


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, (list, tuple)),
    "json": lambda v: True,
}


@dataclass(frozen=True)
class Rule:
    name: ClassVar[str] = "rule"
    message: str = "This value is not accepted."

    def accepts(self, value: Any) -> bool:
        raise NotImplementedError

    def error(self, value: Any) -> str | None:
        return None if self.accepts(value) else self.message

    def describe(self) -> dict[str, Any]:
        return {"rule": self.name, **asdict(self)}


@dataclass(frozen=True)
class Required(Rule):
    name: ClassVar[str] = "required"
    message: str = "Choose a source or write a custom value."

    def accepts(self, value: Any) -> bool:
        return not is_blank(value)


@dataclass(frozen=True)
class OfType(Rule):
    name: ClassVar[str] = "type"
    type: str = "json"
    message: str = ""

    def __post_init__(self) -> None:
        if self.type not in _TYPE_CHECKS:
            raise ValueError(f"unknown value type {self.type!r}")
        if not self.message:
            object.__setattr__(self, "message", TYPE_MESSAGES[self.type])

    def accepts(self, value: Any) -> bool:
        return _TYPE_CHECKS[self.type](value)


@dataclass(frozen=True)
class Pattern(Rule):
    name: ClassVar[str] = "pattern"
    regex: str = ".*"
    message: str = "Does not match the expected format."

    def accepts(self, value: Any) -> bool:
        return not isinstance(value, str) or re.fullmatch(self.regex, value) is not None


@dataclass(frozen=True)
class Length(Rule):
    name: ClassVar[str] = "length"
    min: int | None = None
    max: int | None = None
    message: str = "Has the wrong length."

    def accepts(self, value: Any) -> bool:
        if not isinstance(value, (str, list, tuple)):
            return True
        return ((self.min is None or len(value) >= self.min)
                and (self.max is None or len(value) <= self.max))


@dataclass(frozen=True)
class Range(Rule):
    name: ClassVar[str] = "range"
    min: float | None = None
    max: float | None = None
    message: str = "Is out of range."

    def accepts(self, value: Any) -> bool:
        if not _is_number(value):
            return True
        return ((self.min is None or value >= self.min)
                and (self.max is None or value <= self.max))


@dataclass(frozen=True)
class OneOf(Rule):
    name: ClassVar[str] = "one_of"
    options: tuple[Any, ...] = ()
    message: str = "Is not one of the allowed values."

    def accepts(self, value: Any) -> bool:
        return value in self.options


@dataclass(frozen=True)
class FieldSchema:
    required: bool = True
    rules: tuple[Rule, ...] = ()


#: Per-field additions, keyed by AgentDiscoveryMessage field name, e.g.
#:     "radius": FieldSchema(rules=(Range(min=0.05, max=2.0, message="Between 0.05 and 2 m."),)),
FIELD_SCHEMAS: dict[str, FieldSchema] = {

    "state_sensors" : FieldSchema(required=False),
    "perception_sensors": FieldSchema(required=False),

}


def rules_for(field_name: str, value_type: str) -> tuple[Rule, ...]:
    schema = FIELD_SCHEMAS.get(field_name, FieldSchema())
    required = (Required(),) if schema.required else ()
    return (*required, OfType(type=value_type), *schema.rules)


def validate(value: Any, rules: tuple[Rule, ...]) -> str | None:
    """First failing rule's message. A blank value only answers to Required."""
    if is_blank(value):
        # If Required is in rules, fail with its message; otherwise allow blank
        return next((r.message for r in rules if isinstance(r, Required)), None)

    # If not blank, skip Required and run the rest of the checks
    return next((error for r in rules if not isinstance(r, Required) and (error := r.error(value))), None)

class DiscoveryValidationError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        count = len(errors)
        super().__init__(f"{count} field{'s' if count != 1 else ''} "
                         f"need{'s' if count == 1 else ''} attention.")
