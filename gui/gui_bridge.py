# **************************************************************************
# * gui_bridge.py -- observation layer between the aggregate twin and its console
# *
# * Nothing in here drives the twin's protocol. The bridge wraps a handful of
# * the twin's own bound methods so every discovery, instantiate, mission,
# * twin-state and obstacle message is counted and timestamped as it flows
# * through, and it keeps the discovery payloads (agent footprints) that the
# * twin itself discards after linking.
# *
# * The one thing it *does* write is a mission: the console appends to
# * twin.missions_to_deploy -- the same public queue the tick already drains --
# * and then hands planning to the twin's own executor. No protocol shortcut.
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
    """
    Footprint the console should draw, read out of the discovery message.

    Discovery schemas differ per project, so this reads the common spellings
    and falls back to a sane default per agent kind. If your DiscoveryMessage
    names things differently, this is the only function to edit.
    """
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
        self.events: deque[dict] = deque(maxlen=LOG_SIZE)
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
            "_on_instance_confirmed":    self._on_confirmed,
            "_on_instance_released":     self._on_released,
            "_handle_discovery":         self._on_discovery,
            "_handle_instantiate_reply": self._on_instantiate_reply,
            "_dispatch_mission":         self._on_dispatch,
            "_cancel_mission_on_agent":  self._on_cancel,
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

    def log(self, direction: str, kind: str, peer: str, detail: str, level: str = "info") -> None:
        self.events.append({"t": time.time(), "dir": direction, "kind": kind,
                            "peer": peer, "detail": detail, "level": level})
        if direction == "in":
            self.msgs_in += 1
        elif direction == "out":
            self.msgs_out += 1

    # -- hooks -------------------------------------------------------------
    def _on_discovery(self, payload=None, *a, **k) -> None:
        # Payloads that open a new agent are caught in _request_instance; this
        # only keeps the beacon rate honest for agents already linked.
        for name in list(self.twin._pending_discovery):
            self.peer(name).ch["discovery"].hit()

    def _on_request_instance(self, agent_name, discovery_msg=None, *a, **k) -> None:
        p = self.peer(agent_name)
        p.discovery = discovery_msg or p.discovery
        p.kind = enum_name(getattr(discovery_msg, "kind", None), p.kind)
        p.ch["discovery"].hit()
        p.ch["instantiate_out"].hit()
        p.phase_to("requested")
        self.log("in", "discovery", agent_name, f"beacon · {p.kind}")
        self.log("out", "instantiate", agent_name, "REQUEST")

    def _on_release_instance(self, agent_name, *a, **k) -> None:
        p = self.peer(agent_name)
        p.ch["instantiate_out"].hit()
        self.log("out", "instantiate", agent_name, "CANCEL")

    def _on_instantiate_reply(self, payload=None, *a, **k) -> None:
        self.msgs_in += 0   # counted by the specific transitions below

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

    def _on_dispatch(self, mission, agent_name, *a, **k) -> None:
        p = self.peer(agent_name)
        p.ch["mission_out"].hit()
        self.log("out", "mission", agent_name, f"REQUEST · {mission.mission_id}")

    def _on_cancel(self, mission_id, agent_name, *a, **k) -> None:
        self.peer(agent_name).ch["mission_out"].hit()
        self.log("out", "mission", agent_name, f"CANCEL · {mission_id}", "warn")

    def _on_mission_reply(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin._instance_to_agent.get(instance_name, instance_name)
        self.peer(agent).ch["mission_in"].hit()
        self.log("in", "mission", agent, "reply", "ok")

    def _on_twin_state(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin._instance_to_agent.get(instance_name)
        if agent:
            self.peer(agent).ch["twin_state"].hit()
            self.msgs_in += 1

    def _on_obstacle(self, instance_name, payload=None, *a, **k) -> None:
        agent = self.twin._instance_to_agent.get(instance_name)
        if agent:
            self.peer(agent).ch["obstacle"].hit()
            self.msgs_in += 1
            self.log("in", "obstacle", agent, "observation")

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
    Queues console-authored missions and keeps planning moving.

    Missions are appended to `twin.missions_to_deploy`; the tick drains them
    into `twin.missions`. A watchdog then submits a planning pass on the
    twin's own executor, so `step()` applies the resulting paths and the
    normal mission handshake dispatches them. The console never touches
    the transport.
    """

    def __init__(self, twin: Any, monitor: CommMonitor, poll: float = 1.0) -> None:
        self.twin = twin
        self.monitor = monitor
        self.poll = poll
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        threading.Thread(target=self._watch, name="gui-planner", daemon=True).start()

    def submit(self, mission) -> None:
        self.twin.missions_to_deploy.append(mission)
        self.monitor.log("out", "mission", mission.assigned_ugv or "planner",
                         f"queued · {mission.mission_id}")

    def cancel(self, mission_id: str) -> bool:
        from core_msgs.instance_aggregate.mission import MissionStatus
        mission = next((m for m in self.twin.missions if m.mission_id == mission_id), None)
        if mission is None:
            return False
        if mission.assigned_ugv:
            self.twin._cancel_mission_on_agent(mission_id, mission.assigned_ugv)
        mission.mission_status = MissionStatus.CANCELLED
        return True

    def replan_all(self) -> None:
        self.twin.trigger_global_reassignment()
        self.monitor.log("out", "planner", "fleet", "global reassignment", "warn")

    # -- watchdog ----------------------------------------------------------
    def _watch(self) -> None:
        from core_msgs.instance_aggregate.mission import MissionStatus
        while not self._stop.wait(self.poll):
            try:
                t = self.twin
                if t.missions_to_deploy or t._is_planning_active:
                    continue
                waiting = [m for m in t.missions
                           if m.mission_status == MissionStatus.PENDING
                           and m.mission_id not in t._mission_in_flight]
                if not waiting or not t.fleet.all(include_stale=True):
                    continue
                ugvs = _ugv_list(t)
                if not ugvs:
                    continue
                t._is_planning_active = True
                t._planning_future = t._planner_executor.submit(
                    t.mission_planner.assign_and_plan, t.missions, ugvs)
                self.monitor.log("out", "planner", "fleet",
                                 f"planning {len(waiting)} mission(s)")
            except Exception as exc:                        # keep the thread alive
                self.last_error = str(exc)
                logger.exception("[gui_bridge] planning watchdog")


def _ugv_list(twin) -> list:
    """FleetRegistry.ugvs compares AgentKind against the string 'ugv'; read the
    kind defensively so the console works either way."""
    out = []
    for a in twin.fleet.all(include_stale=True):
        if str(enum_name(a.kind)).lower().endswith("ugv"):
            out.append(a)
    return out
