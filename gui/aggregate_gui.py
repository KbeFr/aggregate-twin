# **************************************************************************
# * aggregate_gui.py -- fleet console for the aggregate twin
# *
# * Read-only over the twin's public state, plus one write path: dispatching
# * a mission, which goes through the twin's normal deploy queue → planner →
# * mission handshake. Start it next to the twin and it stays out of the way:
# *
# *     from gui.aggregate_gui import start_gui
# *     start_gui(twin, port=8081)
# **************************************************************************
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gui.gui_bridge import CommMonitor, MissionGateway, enum_name, shape_of

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_PATH_POINTS = 160

# Shared look and feel ships inside core_msgs (installed with `pip install -e
# core_msgs/` per the Dockerfile), so every node -- aggregate or instance --
# serves the same theme.css from its own process without depending on any
# other node's container being up.
try:
    import core_msgs
    SHARED_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(core_msgs.__file__)), "gui")
except Exception:
    SHARED_STATIC_DIR = None


def start_gui(twin, host: str = "0.0.0.0", port: int = 8081) -> ThreadingHTTPServer:
    """Start the console in a daemon thread and return the server."""
    monitor = CommMonitor(twin)
    gateway = MissionGateway(twin, monitor)
    server = ThreadingHTTPServer((host, port), _make_handler(twin, monitor, gateway))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="aggregate-gui", daemon=True).start()
    logger.info("fleet console listening on http://%s:%d", host, port)
    return server


# ------------------------------------------------------------------ readers
def _xy(state) -> tuple[float, float, float]:
    """Accept State2D, tuple, list or (2,1)/(3,1) ndarray."""
    if state is None:
        return (0.0, 0.0, 0.0)
    if hasattr(state, "x"):
        return (float(state.x), float(state.y), float(getattr(state, "theta", 0.0) or 0.0))
    try:
        flat = [float(v) for v in list(state)[:3]] if not hasattr(state, "flatten") \
            else [float(v) for v in state.flatten()[:3]]
    except Exception:
        return (0.0, 0.0, 0.0)
    flat += [0.0] * (3 - len(flat))
    return (flat[0], flat[1], flat[2])


def _vel(velocity) -> tuple[float, float]:
    if velocity is None:
        return (0.0, 0.0)
    if hasattr(velocity, "linear"):
        return (float(velocity.linear or 0.0), float(velocity.angular or 0.0))
    try:
        v = list(velocity)
        return (float(v[0]), float(v[1]))
    except Exception:
        return (0.0, 0.0)


def _path_points(arr) -> list[list[float]]:
    """A* returns np.array([rx, ry]) goal-first; thin it and hand back [x, y] pairs."""
    try:
        xs, ys = arr[0], arr[1]
    except Exception:
        return []
    n = min(len(xs), len(ys))
    if n == 0:
        return []
    stride = max(1, n // MAX_PATH_POINTS)
    pts = [[float(xs[i]), float(ys[i])] for i in range(0, n, stride)]
    if pts and stride > 1:
        pts.append([float(xs[n - 1]), float(ys[n - 1])])
    return pts


def _obstacle(o, source: str) -> dict:
    return {
        "id": str(getattr(o, "id", "obs")),
        "x": float(getattr(o, "x", 0.0)), "y": float(getattr(o, "y", 0.0)),
        "theta": float(getattr(o, "theta", 0.0) or 0.0),
        "radius": getattr(o, "radius", None),
        "polygon": getattr(o, "polygon", None),
        "dynamic": bool(getattr(o, "is_dynamic", False)),
        "confidence": float(getattr(o, "confidence", 1.0)),
        "source": source,
        "age": round(max(0.0, time.time() - float(getattr(o, "timestamp", time.time()))), 1),
    }


def _sim_time(twin) -> float:
    return round(getattr(twin, "_sim_step", 0) * getattr(twin, "dt", 0.1), 2)


def _header(twin, monitor: CommMonitor) -> dict:
    linked = len(twin._agent_to_instance)
    return {
        "name": getattr(twin, "name", "AggregateTwin"),
        "namespace": getattr(twin, "namespace", "—"),
        "sim_time": _sim_time(twin),
        "step": getattr(twin, "_sim_step", 0),
        "loop_hz": getattr(twin, "loop_freq", 0),
        "uptime": round(time.time() - monitor.started, 1),
        "planning": bool(getattr(twin, "_is_planning_active", False)),
        "linked": linked,
        "pending": len(twin._pending_discovery),
        "lifecycle": "LIVE" if linked else ("BINDING" if twin._pending_discovery else "IDLE"),
    }


# ------------------------------------------------------------- world sheet
def _world_state(twin, monitor: CommMonitor) -> dict:
    wc = twin.world_config
    stale_after = getattr(twin.fleet, "stale_after", 3.0)

    agents = []
    for entry in twin.fleet.all(include_stale=True):
        x, y, theta = _xy(entry.state)
        v, w = _vel(entry.velocity)
        peer = monitor.peers.get(entry.name)
        kind = enum_name(entry.kind, "ugv")
        agents.append({
            "name": entry.name,
            "instance": entry.instance_name,
            "kind": kind,
            "x": x, "y": y, "theta": theta,
            "v": round(v, 3), "w": round(w, 3),
            "battery": entry.battery_pct,
            "arrived": bool(entry.arrive_flag),
            "mission_id": entry.mission_id,
            "age": round(entry.age, 2),
            "stale": entry.age > stale_after,
            "shape": shape_of(peer.discovery if peer else None, kind),
            "path": _path_points(twin._active_paths.get(entry.name)),
        })

    obstacles = [_obstacle(o, "world") for o in wc.static_obstacles]
    for agent_name, obs in twin._instance_obstacles.items():
        for o in (obs if isinstance(obs, (list, tuple, set)) else [obs]):
            if o is not None:
                obstacles.append(_obstacle(o, agent_name))

    missions = []
    for m in twin.missions:
        missions.append({
            "id": m.mission_id,
            "type": enum_name(m.mission_type),
            "posture": enum_name(m.mission_posture),
            "status": enum_name(m.mission_status),
            "goal": list(m.goal_xy) if m.goal_xy else None,
            "waypoints": [list(w) for w in (m.waypoints or [])],
            "target_id": m.target_id,
            "unlock_time": m.unlock_time,
            "assigned": m.assigned_ugv,
            "cost": None if m.last_cost in (None, float("inf")) else round(float(m.last_cost), 2),
            "in_flight": m.mission_id in twin._mission_in_flight,
        })

    return {
        "twin": _header(twin, monitor),
        "world": {"width": wc.width, "height": wc.height,
                  "origin_x": wc.origin_x, "origin_y": wc.origin_y,
                  "resolution": wc.resolution},
        "agents": agents, "obstacles": obstacles, "missions": missions,
        "options": _options(),
    }


def _options() -> dict:
    from core_msgs.instance_aggregate.mission import MissionPosture, MissionType
    return {"types": MissionType.get_names(), "postures": MissionPosture.get_names()}


# ----------------------------------------------------------- network sheet
def _network_state(twin, monitor: CommMonitor, gateway) -> dict:
    stale_after = getattr(twin.fleet, "stale_after", 3.0)
    aggregate = _header(twin, monitor)

    nodes = [{"id": aggregate["name"], "role": "aggregate", "label": aggregate["name"],
              "state": "live", "detail": f"{aggregate['loop_hz']} Hz · {aggregate['linked']} linked"}]
    links, rows = [], []

    for name, peer in sorted(monitor.peers.items()):
        state = monitor.link_state(peer, stale_after)
        instance = peer.instance or twin._agent_to_instance.get(name)
        ts, obs = peer.ch["twin_state"], peer.ch["obstacle"]

        if instance:
            nodes.append({"id": instance, "role": "instance", "label": instance,
                          "state": state, "detail": f"{ts.hz:.1f} Hz twin_state"})
            links.append({"source": aggregate["name"], "target": instance,
                          "kind": "instantiate", "state": state,
                          "detail": f"{peer.ch['instantiate_out'].count}↑ {peer.ch['instantiate_in'].count}↓"})
            links.append({"source": instance, "target": name, "kind": "telemetry",
                          "state": state, "detail": f"{ts.hz:.1f} Hz"})
        nodes.append({"id": name, "role": "agent", "label": name,
                      "state": state, "detail": peer.kind})

        rows.append({
            "agent": name, "instance": instance, "kind": peer.kind,
            "phase": peer.phase, "state": state,
            "since": round(time.time() - peer.since, 1),
            "discovery": peer.ch["discovery"].as_dict(),
            "instantiate": {"out": peer.ch["instantiate_out"].count,
                            "in": peer.ch["instantiate_in"].count},
            "mission": {"out": peer.ch["mission_out"].count,
                        "in": peer.ch["mission_in"].count},
            "twin_state": ts.as_dict(),
            "obstacle": obs.as_dict(),
        })

    counters = {
        "msgs_in": monitor.msgs_in, "msgs_out": monitor.msgs_out,
        "peers": len(monitor.peers),
        "linked": sum(1 for r in rows if r["phase"] == "linked"),
        "silent": sum(1 for r in rows if r["state"] in ("silent", "stale")),
        "in_flight": len(twin._mission_in_flight),
        "planning": aggregate["planning"],
        "stale_after": stale_after,
        "planner_error": gateway.last_error,
    }
    events = [dict(e) for e in list(monitor.events)[-120:]]
    return {"twin": aggregate, "nodes": nodes, "links": links,
            "rows": rows, "counters": counters, "events": events}


# --------------------------------------------------------------- mutations
def _build_mission(twin, spec: dict):
    from core_msgs.instance_aggregate.mission import (
        POSTURE_WEIGHTS, Mission, MissionPosture, MissionType)

    mid = (spec.get("mission_id") or "").strip() or f"m{int(time.time() * 1000) % 1000000}"
    if any(m.mission_id == mid for m in twin.missions):
        raise ValueError(f"mission id '{mid}' is already in use")

    mtype = MissionType[spec.get("type", "GOTO_WAYPOINT")]
    posture_name = spec.get("posture", "COVERAGE")
    posture_enum = MissionPosture[posture_name]
    # POSTURE_WEIGHTS is keyed by name; hand the planner whatever it can index.
    posture = posture_enum if posture_enum in POSTURE_WEIGHTS else posture_name

    goal = spec.get("goal_xy")
    goal_xy = (float(goal[0]), float(goal[1])) if goal else None
    waypoints = [(float(p[0]), float(p[1])) for p in spec.get("waypoints") or []]

    if mtype in (MissionType.GOTO_WAYPOINT, MissionType.TIME_GATED_GOTO) and goal_xy is None:
        raise ValueError("this mission type needs a goal")
    if mtype == MissionType.COVERAGE_PATROL and not waypoints:
        raise ValueError("a patrol needs at least one waypoint")
    if mtype == MissionType.TRACK_TARGET and not spec.get("target_id"):
        raise ValueError("tracking needs a target id")

    return Mission(
        mission_id=mid, mission_type=mtype, mission_posture=posture,
        goal_xy=goal_xy, waypoints=waypoints,
        unlock_time=float(spec.get("unlock_time") or 0.0),
        target_id=int(spec["target_id"]) if spec.get("target_id") else None,
        battery_budget=float(spec["battery_budget"]) if spec.get("battery_budget") else None,
    )


# ----------------------------------------------------------------- handler
def _make_handler(twin, monitor: CommMonitor, gateway: MissionGateway):

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "AggregateConsole/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            logger.debug("gui %s", fmt % args)

        # -- plumbing ------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=str).encode(),
                       "application/json; charset=utf-8")

        def _static(self, name: str) -> None:
            # Node-local static first, shared core_msgs assets (theme.css) second.
            for base in filter(None, (STATIC_DIR, SHARED_STATIC_DIR)):
                path = os.path.normpath(os.path.join(base, name))
                if path.startswith(base) and os.path.isfile(path):
                    ctype = {".html": "text/html", ".css": "text/css",
                             ".js": "text/javascript"}.get(os.path.splitext(path)[1], "text/plain")
                    with open(path, "rb") as fh:
                        return self._send(200, fh.read(), f"{ctype}; charset=utf-8")
            self._json({"error": "not found"}, 404)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        # -- routes --------------------------------------------------------
        def do_GET(self):                                    # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if path == "/":
                    return self._static("index.html")
                if path == "/healthz":
                    return self._json({"status": "ok", **_header(twin, monitor)})
                if path == "/api/world":
                    return self._json(_world_state(twin, monitor))
                if path == "/api/network":
                    return self._json(_network_state(twin, monitor, gateway))
                if path.startswith("/static/") or path.endswith((".html", ".css", ".js")):
                    return self._static(os.path.basename(path))
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                logger.exception("GET %s", path)
                self._json({"error": str(exc)}, 500)

        def do_POST(self):                                   # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                if path == "/api/missions":
                    mission = _build_mission(twin, self._body())
                    gateway.submit(mission)
                    return self._json({"ok": True, "mission_id": mission.mission_id}, 201)
                if path == "/api/missions/cancel":
                    mid = self._body().get("mission_id", "")
                    ok = gateway.cancel(mid)
                    return self._json({"ok": ok}, 200 if ok else 404)
                if path == "/api/replan":
                    gateway.replan_all()
                    return self._json({"ok": True})
                if path == "/api/release":
                    twin._release_instance(self._body().get("agent", ""))
                    return self._json({"ok": True})
                self._json({"error": "not found"}, 404)
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                logger.exception("POST %s", path)
                self._json({"error": str(exc)}, 500)

    return ConsoleHandler