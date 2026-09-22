from __future__ import annotations

import logging
import time
from typing import Callable


logger = logging.getLogger(__name__)


class UnlinkedRegistry:
    """
    Aggregate-side set of unlinked nodes.
    Agents waiting on an instance, or instances waiting on an agent.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._register: dict[str, object] = {}
        self._last_seen: dict[str, float] = {}
        # The aggregate runs on sim time, so the pool has to age on the same clock
        # or a fast sim never evicts anything and a slow one evicts everything.
        self.clock = clock

    def register(self, name: str, payload: object) -> None:
        """payload is the node's discovery message, not a handshake object."""
        self._register[name] = payload
        self._last_seen[name] = self.clock()

    def get(self, name: str, default=None):
        return self._register.get(name, default)

    def remove(self, name: str) -> object | None:
        """Pop a specific entry once it's actually been linked (or its
        attempt failed and it's being re-registered fresh elsewhere)."""
        self._last_seen.pop(name, None)
        return self._register.pop(name, None)

    # -- heartbeat / liveness ------------------------------------------------

    def touch(self, name: str) -> bool:
        """Record a heartbeat for `name`. Returns False if it isn't registered here."""
        if name not in self._register:
            return False
        self._last_seen[name] = self.clock()
        return True

    def age(self, name: str) -> float | None:
        """Seconds since `name` last registered or heartbeated, or None if unknown."""
        last = self._last_seen.get(name)
        if last is None:
            return None
        return self.clock() - last

    def is_stale(self, name: str, timeout: float) -> bool:
        """True if `name` is unknown or hasn't heartbeated within `timeout` seconds."""
        age = self.age(name)
        return age is None or age > timeout

    def stale_names(self, timeout: float) -> list[str]:
        """Names with no heartbeat inside `timeout` seconds, oldest-first."""
        now = self.clock()
        stale = [(name, now - last) for name, last in self._last_seen.items() if now - last > timeout]
        stale.sort(key=lambda pair: pair[1], reverse=True)
        return [name for name, _ in stale]

    def names(self) -> list[str]:
        return list(self._register)

    def check_health(self) -> bool:
        return len(self._register) > 0

    def size(self) -> int:
        return len(self._register)

    def first(self):
        """Pop the oldest (first-registered) entry as (name, discovery), or None if empty"""
        name = next(iter(self._register), None)
        if name is None:
            return None
        self._last_seen.pop(name, None)
        return name, self._register.pop(name)

    def __bool__(self):
        return len(self._register) > 0

    def __contains__(self, name: str) -> bool:
        return name in self._register

    def __getitem__(self, name: str):
        return self._register[name]

    def __iter__(self):
        return iter(self._register.items())