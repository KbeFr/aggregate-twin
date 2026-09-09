# **************************************************************************
# * gui_bridge.py -- observation layer between the aggregate twin and its console
# *
# * Nothing in here drives the twin's protocol. The bridge wraps a handful of
# * the twin's own bound methods so every discovery, instantiate, mission,
# * twin-state and obstacle message is counted and timestamped as it flows
# * through, and it keeps the discovery payloads (agent footprints) that the
# * twin itself discards after linking.
# *
# * Writes go through MissionGateway, which queues onto the twin's inbox so
# * they run on the step thread. Nothing here touches the transport.
# **************************************************************************
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

LOG_SIZE = 400
RATE_WINDOW = 20


# ---------------------------------------------------------------- utilities
def _wrap(obj: Any, name: str, after: Callable[..., None]) -> bool:
    """Wrap a bound method so `after(*args, **kwargs)` runs once it returns."""
    original = getattr(obj, name, None)
    if original is None or getattr(original, "_gui_wrapped", False):
        return False

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            after(*args, **kwargs)
        except Exception:                                   # never break the twin
            logger.exception("[gui_bridge] instrumentation failed on %s", name)
        return result

    wrapper._gui_wrapped = True                             # type: ignore[attr-defined]
    setattr(obj, name, wrapper)
    return True


def enum_name(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    return str(getattr(value, "name", getattr(value, "value", value)))


def shape_of(discovery: Any, kind: str) -> dict:
    """Footprint the console should draw, read out of the discovery message."""
    if discovery is not None:
        src = getattr(discovery, "shape", None) or discovery
        poly = _first(src, "polygon", "vertices", "footprint")
        if poly:
            try:
                return {"type": "polygon", "points": [[float(p[0]), float(p[1])] for p in poly]}
            except Exception:
                pass
        length = _first(src, "length", "size_x")
        width = _first(src, "width", "size_y")
        if length and width:
            return {"type": "rect", "length": float(length), "width": float(width)}
        radius = _first(src, "radius", "footprint_radius")
        if radius:
            return {"type": "circle", "radius": float(radius)}

    if str(kind).lower().endswith("uav"):
        return {"type": "rotor", "radius": 0.30}
    return {"type": "rect", "length": 0.44, "width": 0.30}


def _first(obj: Any, *names: str):
    for n in names:
        v = getattr(obj, n, None) if not isinstance(obj, dict) else obj.get(n)
        if v not in (None, 0, [], ()):
            return v
    return None


def _agent_name_of(msg: Any) -> Optional[str]:
    """DiscoveryMessage field spelling differs per project."""
    for attr in ("agent_name", "robot_name", "name"):
        v = getattr(msg, attr, None)
        if v:
            return str(v)
    return None


# ------------------------------------------------------------------- stats
class Channel:
    """Message counters and arrival rate for one direction of one link."""

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

    def as_dict(self) -> dict:
        return {"count": self.count, "hz": round(self.hz, 2),
                "age": None if self.age is None else round(self.age, 2)}


class Peer:
    """Everything the console knows about one agent / instance pair."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.instance: Optional[str] = None
        self.kind: str = "ugv"
        self.discovery: Any = None
        self.phase = "discovered"        # discovered → requested → linked → released
        self.since = time.time()
        self.nacks = 0
        self.ch = {k: Channel() for k in
                   ("discovery", "instantiate_out", "instantiate_in",
                    "mission_out", "mission_in", "twin_state", "obstacle")}

    def phase_to(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.since = time.time()


class CommMonitor:
    """Read-only observer of the aggregate twin's message flow."""

    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.started = time.time()
        self.peers: dict[str, Peer] = {}
        # Two separate logs, not one filtered view: obstacle/twin_state fire
        # at sensor rate and would otherwise evict every handshake entry out
        # of a single shared deque within seconds.
        self.events: deque[dict] = deque(maxlen=LOG_SIZE)            # discovery/instantiate/mission
        self.telemetry_events: deque[dict] = deque(maxlen=LOG_SIZE)  # obstacle/twin_state
        self.msgs_in = 0
        self.msgs_out = 0
        self._lock = threading.Lock()
        self._attach()

    # -- wiring ------------------------------------------------------------
    def _attach(self) -> None:
        t = self.twin
        hooks = {
            "_request_instance":         self._on_request_instance,
            "_release_instance":         self._on_release_instance,
            "_handle_instantiate_reply": self._on_instantiate_reply,
            "_on_instance_confirmed":    self._on_confirmed,
            "_on_instance_released":     self._on_released,
            "_handle_discovery":         self._on_discovery,
            "_send_session_out":         self._on_dispatch,
            "cancel_mission":            self._on_cancel,
            "_handle_mission_reply":     self._on_mission_reply,
            "_handle_twin_state":        self._on_twin_state,
            "_handle_obstacle":          self._on_obstacle,
        }
        missing = [name for name, cb in hooks.items() if not _wrap(t, name, cb)]
        if missing:
            logger.warning("[gui_bridge] could not instrument: %s", ", ".join(missing))

    # -- bookkeeping -------------------------------------------------------
    def peer(self, agent: str) -> Peer:
        with self._lock:
            p = self.peers.get(agent)
            if p is None:
                p = self.peers[agent] = Peer(agent)
            return p

    def snapshot(self) -> list[tuple[str, Peer]]:
        """Stable list for the HTTP thread to iterate. Peers are mutated by twin
        callbacks, so never iterate self.peers directly from a request."""
        with self._lock:
            return list(self.peers.items())

    def log(self, direction: str, kind: str, peer: str, detail: str, level: str = "info",
             channel: str = "handshake") -> None:
        target = self.telemetry_events if channel == "telemetry" else self.events
        target.append({"t": time.time(), "dir": direction, "kind": kind,
                       "peer": peer, "detail": detail, "level": level})
        if direction == "in":
            self.msgs_in += 1
        elif direction == "out":
            self.msgs_out += 1

    # -- hooks -------------------------------------------------------------
    def _on_discovery(self, msg=None, *a, **k) -> None:
        """Count the beacon against the agent that actually sent it. Counting it
        against every pending agent inflated each one's rate by the number of
        agents currently binding."""
        name = _agent_name_of(msg)
        if name:
            self.peer(name).ch["discovery"].hit()

    def _on_request_instance(self, agent_name, discovery_msg=None, *a, **k) -> None:
        p = self.peer(agent_name)
        p.discovery = discovery_msg or p.discovery
        p.kind = enum_name(getattr(discovery_msg, "kind", None), p.kind)
        p.ch["instantiate_out"].hit()
        p.phase_to("requested")
        self.log("in", "discovery", agent_name, f"beacon · {p.kind}")
        self.log("out", "instantiate", agent_name, "REQUEST")

    def _on_release_instance(self, agent_name, *a, **k) -> None:
        p = self.peer(agent_name)
        p.ch["instantiate_out"].hit()
        self.log("out", "instantiate", agent_name, "CANCEL")

    def _on_confirmed(self, agent_name, instance_name, *a, **k) -> None:
        p = self.peer(agent_name)
        p.instance = instance_name
        p.ch["instantiate_in"].hit()
        p.phase_to("linked")
        self.log("in", "instantiate", agent_name, f"ACK · {instance_name}", "ok")

    def _on_released(self, agent_name, instance_name=None, *a, **k) -> None:
        p = self.peer(agent_name)
        p.ch["instantiate_in"].hit()
        p.phase_to("released")
        self.log("in", "instantiate", agent_name, "CANCEL_ACK", "warn")

    def _on_instantiate_reply(self, env=None, *a, **k) -> None:
        """_handle_instantiate_reply sees every raw ACK/NACK/CANCEL_ACK an
        instance sends back - _on_confirmed/_on_released only fire once the
        *outcome* is decided, so without this, a losing instance's NACK (or
        a winner's initial ACK, before it's actually confirmed) never showed
        up anywhere in the log at all."""
        if env is None:
            return
        # the 'instantiate' topic is inout for the aggregate itself, so it
        # also receives its own outgoing envelopes as an echo - same guard
        # _handle_instantiate_reply applies internally, needed here too
        # since the wrapper fires regardless of what the original did.
        if getattr(env, "sender", None) == getattr(self.twin, "name", None):
            return
        agent_name = getattr(env, "agent_name", None)
        if not agent_name:
            return
        status = enum_name(getattr(env, "handshake_status", None))
        sender = getattr(env, "sender", "?")
        p = self.peer(agent_name)
        if status == "NACK":
            p.nacks += 1
        level = "warn" if status == "NACK" else ("ok" if status == "ACK" else "info")
        self.log("in", "instantiate", agent_name, f"{status.lower()} from {sender}", level)

    def _on_dispatch(self, out=None, *a, **k) -> None:
        """_send_session_out takes {agent_name: MissionEnvelope}."""
        for agent_name, env in (out or {}).items():
            self.peer(agent_name).ch["mission_out"].hit()
            self.log("out", "mission", agent_name,
                     f"{enum_name(getattr(env, 'handshake_status', None))} · "
                     f"{getattr(env, 'mission_id', '?')}")

    def _on_cancel(self, mission_id, *a, **k) -> None:
        """cancel_mission(mission_id, reason=...) -- no agent argument."""
        session = getattr(self.twin, "_mission_sessions", {}).get(mission_id)
        agent = getattr(session, "committed_agent", None) if session else None
        if agent:
            self.peer(agent).ch["mission_out"].hit()
        self.log("out", "mission", agent or "—", f"CANCEL · {mission_id}", "warn")

    def _on_mission_reply(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin.fleet.agent_of(instance_name)
        if not agent:
            # A reply from an instance we have already released. Never create a
            # peer keyed None -- that breaks sorting the peer table forever.
            self.log("in", "mission", str(instance_name), "reply from unmapped instance", "warn")
            return
        status = enum_name(getattr(payload, "handshake_status", None))
        mission_id = getattr(payload, "mission_id", "?")
        level = "warn" if status == "NACK" else "ok"
        self.peer(agent).ch["mission_in"].hit()
        self.log("in", "mission", agent, f"{status.lower()} · {mission_id}", level)

    def _on_twin_state(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin.fleet.agent_of(instance_name)
        if agent:
            self.peer(agent).ch["twin_state"].hit()
            self.msgs_in += 1
            self.log("in", "twin_state", agent, "telemetry", channel="telemetry")

    def _on_obstacle(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin.fleet.agent_of(instance_name)
        if agent:
            self.peer(agent).ch["obstacle"].hit()
            self.msgs_in += 1
            self.log("in", "obstacle", agent, "observation", channel="telemetry")

    # -- reporting ---------------------------------------------------------
    def link_state(self, p: Peer, telemetry_stale_after: float) -> str:
        if p.phase in ("discovered", "requested"):
            return "pending"
        if p.phase == "released":
            return "released"
        age = p.ch["twin_state"].age
        if age is None:
            return "silent"
        return "live" if age <= telemetry_stale_after else "stale"


# ------------------------------------------------------------ mission gate
class MissionGateway:
    """
    Console write path.

    """

    def __init__(self, twin: Any, monitor: CommMonitor, poll: float = 1.0) -> None:
        self.twin = twin
        self.monitor = monitor
        self.last_error: Optional[str] = None
        self._warned_direct = False

    # -- plumbing ----------------------------------------------------------
    def _command(self, kind: str, direct: Callable[[], Any], **kwargs) -> None:
        """Prefer the twin's queued command path; fall back to a direct call."""
        submit = getattr(self.twin, "submit_command", None)
        if callable(submit):
            submit(kind, **kwargs)
            return
        if not self._warned_direct:
            self._warned_direct = True
            logger.warning(
                "[gui_bridge] twin has no submit_command(); console writes run on "
                "the HTTP thread and can race step(). See LAYOUT notes.")
        direct()

    # -- actions -----------------------------------------------------------
    def submit(self, mission) -> None:
        self._command("add_mission",
                      lambda: self.twin.add_mission(mission),
                      mission=mission)
        self.monitor.log("out", "mission", mission.assigned_ugv or "planner",
                         f"queued · {mission.mission_id}")

    def cancel(self, mission_id: str) -> bool:
        from core_msgs.instance_aggregate.mission import MissionStatus
        mission = next((m for m in self.twin.missions if m.mission_id == mission_id), None)
        if mission is None:
            return False
        self._command("cancel_mission",
                      lambda: self.twin.cancel_mission(mission_id),
                      mission_id=mission_id)
        mission.mission_status = MissionStatus.CANCELLED
        return True

    def confirm_discovery(self, agent_name: str, fields: dict) -> None:
        """Build the operator-resolved DiscoveryMessage and hand it to the
        twin's gui_trigger_discovery(), the same entrypoint check_discovery()
        documents for the human-review path."""
        from core_msgs.global_msgs.global_payloads import DiscoveryMessage
        from core_msgs.agents_contract import AgentKind

        fields = dict(fields)
        if isinstance(fields.get("kind"), str):
            fields["kind"] = AgentKind(fields["kind"])
        msg = DiscoveryMessage(**fields)

        self._command("gui_trigger_discovery",
                      lambda: self.twin.gui_trigger_discovery(msg),
                      discovery_msg=msg)
        self.monitor.log("out", "discovery", agent_name, "confirmed via console", "ok")

    def release(self, agent_name: str) -> None:
        if not agent_name:
            raise ValueError("no agent given")
        self._command("release_instance",
                      lambda: self.twin._release_instance(agent_name),
                      agent_name=agent_name)
        self.monitor.log("out", "instantiate", agent_name, "release requested", "warn")

    def replan_all(self) -> None:
        fn = getattr(self.twin, "trigger_global_reassignment", None)
        if not callable(fn):
            raise ValueError("this twin has no global reassignment")
        self._command("replan", fn)
        self.monitor.log("out", "planner", "fleet", "global reassignment", "warn")