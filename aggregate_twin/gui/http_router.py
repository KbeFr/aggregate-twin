"""A minimal path router and the errors the console API answers with."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote


class ApiError(Exception):
    """An error meant for the operator: the message is shown as-is."""

    status = 400

    def __init__(self, message: str, *, status: int | None = None,
                 fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status
        self.fields = fields or {}

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": str(self)}
        if self.fields:
            body["fields"] = self.fields
        return body


class NotFound(ApiError):
    status = 404


class Conflict(ApiError):
    status = 409


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    params: dict[str, str]
    body: dict[str, Any] = field(default_factory=dict)


Handler = Callable[..., Any]


class Router:
    """Routes like "/api/discoveries/{agent}/resolve"; placeholders match one segment."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def get(self, pattern: str) -> Callable[[Handler], Handler]:
        return self._register("GET", pattern)

    def post(self, pattern: str) -> Callable[[Handler], Handler]:
        return self._register("POST", pattern)

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, str]] | None:
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            found = regex.match(path)
            if found:
                return handler, {k: unquote(v) for k, v in found.groupdict().items()}
        return None

    def _register(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def register(handler: Handler) -> Handler:
            self._routes.append((method, regex, handler))
            return handler

        return register
