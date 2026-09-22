"""
Read-only instrumentation between the aggregate twin and its console.

Wraps a few of the twin's and the link manager's own methods so every discovery,
handshake envelope, heartbeat, twin state and obstacle is counted and timestamped
as it passes. Nothing here changes what the twin does, and a failing hook is
logged, never raised.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from aggregate_twin.gui.view_helpers import enum_name, enum_value
from core_msgs.global_msgs.global_payloads import AgentDiscoveryMessage, RegisteredMessage

logger = logging.getLogger(__name__)

LOG_SIZE = 400
RATE_WINDOW = 20


def _wrap(obj: Any, name: str, *, before: Callable | None = None,
          after: Callable | None = None) -> bool:
    original = getattr(obj, name, None)
    if original is None or getattr(original, "_monitored", False):
        return False

    def call_hook(hook, args, kwargs):
        try:
            hook(*args, **kwargs)
        except Exception:
            logger.exception("[comm_monitor] hook on %s failed", name)

    def wrapper(*args, **kwargs):
        if before:
            call_hook(before, args, kwargs)
        result = original(*args, **kwargs)
        if after:
            call_hook(after, args, kwargs)
        return result

    wrapper._monitored = True                               # type: ignore[attr-defined]
    setattr(obj, name, wrapper)
    return True


def _status(env: Any) -> str:
    for attr in ("status", "handshake_status"):
        value = getattr(env, attr, None)
        if value is not None:
            return enum_name(value).lower()
    return "?"


def _name_of(msg: Any) -> Optional[str]:
    for attr in ("name", "agent_name", "sender"):
        value = getattr(msg, attr, None)
        if value:
            return str(value)
    return None


class Channel:
    """Message count and arrival rate for one direction of one link."""

    def __init__(self) -> None:
        self.count = 0
        self.last: Optional[float] = None
        self._stamps: deque[float] = deque(maxlen=RATE_WINDOW)

    def hit(self) -> None:
        now = time.time()
        self.count += 1
        self.last = now
        self._stamps.append(now)

    @property
    def hz(self) -> float:
        if len(self._stamps) < 2:
            return 0.0
        span = self._stamps[-1] - self._stamps[0]
        return (len(self._stamps) - 1) / span if span > 1e-6 else 0.0

    @property
    def age(self) -> Optional[float]:
        return None if self.last is None else time.time() - self.last

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "hz": round(self.hz, 2),
                "age": None if self.age is None else round(self.age, 2)}


class Peer:
    """Everything the console has seen of one agent: discovered, review, requested,
    linked, released or rejected."""

    CHANNELS = ("discovery", "instantiate_out", "instantiate_in",
                "mission_out", "mission_in", "twin_state", "obstacle")

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.instance: Optional[str] = None
        self.kind = "ugv"
        self.phase = "discovered"
        self.since = time.time()
        self.nacks = 0
        self.ch = {name: Channel() for name in self.CHANNELS}

    def phase_to(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.since = time.time()


class CommMonitor:

    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.started = time.time()
        self.peers: dict[str, Peer] = {}
        self.heartbeats: dict[str, Channel] = {}
        # Separate logs: telemetry fires at sensor rate and would evict every
        # handshake entry from a shared deque within seconds.
        self.events: deque[dict] = deque(maxlen=LOG_SIZE)
        self.telemetry_events: deque[dict] = deque(maxlen=LOG_SIZE)
        self.msgs_in = 0
        self.msgs_out = 0
        self._lock = threading.Lock()
        self._attach()

    # -- wiring ------------------------------------------------------------
    def _attach(self) -> None:
        twin, links = self.twin, self.twin.link_manager
        hooks = (
            (twin, "_flush", self._before_flush, None),
            (twin, "_handle_discovery", None, self._on_discovery),
            (twin, "_handle_instantiate_reply", self._on_link_reply, None),
            (twin, "_handle_activate_reply", self._on_link_reply, None),
            (twin, "_handle_mission_reply", self._on_mission_reply, None),
            (twin, "_handle_twin_state", None, self._on_twin_state),
            (twin, "_handle_obstacle", None, self._on_obstacle),
            (twin, "_handle_heartbeat", None, self._on_heartbeat),
            (links, "on_agent_incomplete", None, self._on_needs_review),
            (links, "on_agent_discovered", None, self._on_completed),
            (links, "gui_confirm_agent", None, self._on_completed),
            (links, "gui_reject_agent", None, self._on_rejected),
            (links, "on_link", None, self._on_linked),
            (links, "on_release", self._on_released, None),
        )
        missing = [f"{type(obj).__name__}.{name}" for obj, name, before, after in hooks
                   if not _wrap(obj, name, before=before, after=after)]
        if missing:
            logger.warning("[comm_monitor] could not instrument: %s", ", ".join(missing))

    # -- bookkeeping -------------------------------------------------------
    def peer(self, agent: str) -> Peer:
        with self._lock:
            found = self.peers.get(agent)
            if found is None:
                found = self.peers[agent] = Peer(agent)
            return found

    def find(self, agent: str) -> Optional[Peer]:
        with self._lock:
            return self.peers.get(agent)

    def snapshot(self) -> list[tuple[str, Peer]]:
        """Stable copy for HTTP threads; the step thread mutates peers."""
        with self._lock:
            return list(self.peers.items())

    def heartbeat_hz(self, name: str) -> float:
        channel = self.heartbeats.get(name)
        return channel.hz if channel else 0.0

    def log(self, direction: str, kind: str, peer: str, detail: str,
            level: str = "info", channel: str = "handshake") -> None:
        target = self.telemetry_events if channel == "telemetry" else self.events
        target.append({"t": time.time(), "dir": direction, "kind": kind,
                       "peer": peer, "detail": detail, "level": level})
        if direction == "in":
            self.msgs_in += 1
        elif direction == "out":
            self.msgs_out += 1

    def link_state(self, peer: Peer, stale_after: float) -> str:
        if peer.phase in ("discovered", "review", "requested"):
            return "pending"
        if peer.phase in ("released", "rejected"):
            return "released"
        age = peer.ch["twin_state"].age
        if age is None:
            return "silent"
        return "live" if age <= stale_after else "stale"

    # -- outbound ------------------------------------------------------------
    def _before_flush(self, *_args, **_kwargs) -> None:
        twin = self.twin
        if twin.transport is None:            # nothing leaves, and the outbox is kept
            return
        for env in list(twin.link_manager.outbox):
            self._on_link_out(env)
        for env in list(twin.mission_manager.outbox):
            self._on_mission_out(env)

    def _on_link_out(self, env: Any) -> None:
        agent, target, status = str(env.id), getattr(env, "target", None), _status(env)
        peer = self.peer(agent)
        peer.ch["instantiate_out"].hit()
        if status == "request" and peer.phase != "linked":
            peer.phase_to("requested")
        topic = "activate" if self.twin.instance_discovery and target else "instantiate"
        self.log("out", topic, agent, f"{status} to {target or 'any instance'}")

    def _on_mission_out(self, env: Any) -> None:
        target = getattr(env, "target", None)
        agent = self.twin.fleet.agent_of(target) if target else None
        if agent:
            self.peer(agent).ch["mission_out"].hit()
        self.log("out", "mission", agent or str(target or "—"), f"{_status(env)} {env.id}")

    # -- inbound -------------------------------------------------------------
    def _on_discovery(self, msg: Any = None, *_args, **_kwargs) -> None:
        name = _name_of(msg)
        if not name:
            return
        if type(msg).__name__.startswith("Instance"):
            self.log("in", "discovery", name, "instance announced")
            return
        peer = self.peer(name)
        peer.ch["discovery"].hit()
        peer.kind = enum_value(getattr(msg, "kind", None), peer.kind)
        if peer.phase in ("released", "rejected"):
            peer.phase_to("discovered")
        self.log("in", "discovery", name, f"agent announced, {peer.kind}")

    def _on_needs_review(self, agent_name: str, *_args, **_kwargs) -> None:
        self.peer(agent_name).phase_to("review")
        self.log("in", "discovery", agent_name, "incomplete, waiting for review", "warn")

    def _on_completed(self, discovery: Any = None, *_args, **_kwargs) -> None:
        name = _name_of(discovery)
        if name and self.peer(name).phase in ("review", "released", "rejected"):
            self.peer(name).phase_to("discovered")

    def _on_rejected(self, agent_name: str, *_args, **_kwargs) -> None:
        self.peer(agent_name).phase_to("rejected")

    def _on_link_reply(self, *args, **kwargs) -> None:
        env = kwargs.get("env") or (args[-1] if args else None)

        # the first active message is for registering // LOOPBACK
        if isinstance(env, RegisteredMessage):
            return

        if env is None or getattr(env, "sender", None) == self.twin.name:
            return
        status, sender = _status(env), getattr(env, "sender", "?")
        peer = self.peer(str(env.id))
        peer.ch["instantiate_in"].hit()
        if status == "nack":
            peer.nacks += 1
        level = "warn" if status == "nack" else ("ok" if status == "ack" else "info")
        self.log("in", "instantiate", peer.agent, f"{status} from {sender}", level)

    def _on_mission_reply(self, *args, **kwargs) -> None:
        env = kwargs.get("env") or (args[-1] if args else None)
        if env is None or getattr(env, "sender", None) == self.twin.name:
            return
        sender = getattr(env, "sender", None)
        agent = self.twin.fleet.agent_of(sender) if sender else None
        status = _status(env)
        if agent:
            self.peer(agent).ch["mission_in"].hit()
        self.log("in", "mission", agent or str(sender or "—"), f"{status} {env.id}",
                 "warn" if status == "nack" else "ok")

    def _on_twin_state(self, instance_name: str, *_args, **_kwargs) -> None:
        agent = self.twin.fleet.agent_of(instance_name)
        if agent:
            self.peer(agent).ch["twin_state"].hit()
            self.log("in", "twin_state", agent, "telemetry", channel="telemetry")

    def _on_obstacle(self, instance_name: str, *_args, **_kwargs) -> None:
        agent = self.twin.fleet.agent_of(instance_name)
        if agent:
            self.peer(agent).ch["obstacle"].hit()
            self.log("in", "obstacle", agent, "observation", channel="telemetry")

    def _on_heartbeat(self, msg: Any = None, sender: Any = None, *_args, **_kwargs) -> None:
        name = str(sender) if sender else _name_of(msg)
        if not name:
            return
        self.heartbeats.setdefault(name, Channel()).hit()
        self.log("in", "heartbeat", name, "alive", channel="telemetry")

    # -- lifecycle -----------------------------------------------------------
    def _on_linked(self, agent_name: str, instance_name: str, *_args, **_kwargs) -> None:
        if self.twin.fleet.instance_of(agent_name) != instance_name:
            return                              # on_link refused it
        peer = self.peer(agent_name)
        peer.instance = instance_name
        peer.phase_to("linked")
        self.log("in", "instantiate", agent_name, f"linked to {instance_name}", "ok")

    def _on_released(self, agent_name: str, instance_name: Any = None, *_args, **_kwargs) -> None:
        peer = self.peer(agent_name)
        peer.phase_to("released")
        where = instance_name or peer.instance or "its instance"
        self.log("in", "instantiate", agent_name, f"released from {where}", "warn")
