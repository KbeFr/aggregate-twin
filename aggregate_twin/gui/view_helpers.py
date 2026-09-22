"""Converters shared by the console views: twin objects in, JSON-safe values out."""
from __future__ import annotations

import time
from typing import Any

MAX_PATH_POINTS = 160


def enum_name(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    return str(getattr(value, "name", getattr(value, "value", value)))


def enum_value(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    return str(getattr(value, "value", value))


def xy(state: Any) -> tuple[float, float, float]:
    """Accepts State2D, a tuple or list, or a (2,1)/(3,1) ndarray."""
    if state is None:
        return (0.0, 0.0, 0.0)
    if hasattr(state, "x"):
        return (float(state.x), float(state.y), float(getattr(state, "theta", 0.0) or 0.0))
    try:
        values = state.flatten() if hasattr(state, "flatten") else list(state)
        flat = [float(v) for v in list(values)[:3]]
    except Exception:
        return (0.0, 0.0, 0.0)
    flat += [0.0] * (3 - len(flat))
    return (flat[0], flat[1], flat[2])


def velocity(value: Any) -> tuple[float, float]:
    if value is None:
        return (0.0, 0.0)
    if hasattr(value, "linear"):
        return (float(value.linear or 0.0), float(value.angular or 0.0))
    try:
        v = list(value)
        return (float(v[0]), float(v[1]))
    except Exception:
        return (0.0, 0.0)


def route_points(path: Any) -> list[list[float]]:
    """A route as [[x, y], ...], thinned to MAX_PATH_POINTS.

    Accepts A*'s (2, N) column layout as well as a list of points, and always
    returns a list: the console reads `path` unconditionally.
    """
    try:
        if path is None or len(path) == 0:
            return []
        first = path[0]
        columns = len(path) == 2 and hasattr(first, "__len__") and len(first) > 2
        points = ([[float(x), float(y)] for x, y in zip(path[0], path[1])] if columns
                  else [[float(p[0]), float(p[1])] for p in path])
    except Exception:
        return []
    stride = max(1, len(points) // MAX_PATH_POINTS)
    thinned = points[::stride]
    if stride > 1 and thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


def obstacle(o: Any, source: str) -> dict[str, Any]:
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


def shape_of(discovery: Any, kind: str) -> dict[str, Any]:
    """Footprint to draw, read out of the discovery message."""
    if discovery is not None:
        src = getattr(discovery, "shape", None) or discovery
        polygon = _first(src, "polygon", "vertices", "footprint")
        if polygon:
            try:
                return {"type": "polygon", "points": [[float(p[0]), float(p[1])] for p in polygon]}
            except Exception:
                pass
        length, width = _first(src, "length", "size_x"), _first(src, "width", "size_y")
        if length and width:
            return {"type": "rect", "length": float(length), "width": float(width)}
        radius = _first(src, "radius", "footprint_radius")
        if radius:
            return {"type": "circle", "radius": float(radius)}
    if str(kind).lower().endswith("uav"):
        return {"type": "rotor", "radius": 0.30}
    return {"type": "rect", "length": 0.44, "width": 0.30}


def header(twin: Any, monitor: Any) -> dict[str, Any]:
    linked = twin.fleet.amount_linked()
    review = len(twin.discoveries_gui)
    pooled = sum(1 for _, discovery in list(twin.unlinked_agents) if discovery is not None)
    return {
        "name": getattr(twin, "name", "AggregateTwin"),
        "namespace": getattr(twin, "namespace", "—"),
        "sim_time": round(getattr(twin, "_sim_step", 0) * getattr(twin, "dt", 0.1), 2),
        "step": getattr(twin, "_sim_step", 0),
        "loop_hz": getattr(twin, "loop_freq", 0),
        "uptime": round(time.time() - monitor.started, 1),
        "linked": linked,
        "pooled": pooled,
        "review": review,
        "lifecycle": "LIVE" if linked else ("BINDING" if pooled or review else "IDLE"),
        "autocomplete": bool(getattr(twin, "autocomplete", False)),
    }


def _first(obj: Any, *names: str) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value not in (None, 0, [], ()):
            return value
    return None
