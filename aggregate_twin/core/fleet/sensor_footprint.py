"""
sensor_footprint.py

What an agent's sensors can see, learned from what it reports. The aggregate never gets
a sensor spec, so every detection an agent reports is taken as proof that its sensors
reach that point. The footprint is the convex hull of those points in the agent's own
body frame, so it moves and turns with the agent.

Kept light on purpose: a detection inside the hull (or within `margin` of its border)
is dropped without touching it, and the hull is capped at `max_vertices` by removing
the vertex that adds the least area.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points) -> list[Point]:
    """Andrew's monotone chain: counter-clockwise, collinear points dropped."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts
    lower: list[Point] = []
    upper: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def outside_distance(hull: list[Point], p: Point) -> float:
    """0 inside a counter-clockwise hull, else the distance to its border."""
    n = len(hull)
    if n == 0:
        return math.inf
    if n == 1:
        return math.hypot(p[0] - hull[0][0], p[1] - hull[0][1])
    if n >= 3 and all(_cross(hull[i], hull[(i + 1) % n], p) >= 0 for i in range(n)):
        return 0.0
    edges = range(n) if n >= 3 else range(1)
    return min(_segment_distance(p, hull[i], hull[(i + 1) % n]) for i in edges)


@dataclass
class SensorFootprint:
    margin: float = 0.05          # [m] growth below this is noise, not reach
    max_vertices: int = 16
    max_range: float = 20.0       # [m] a report farther than this is treated as an outlier
    # Seeded with the body centre, so a forward camera grows into a wedge from the agent
    hull: list[Point] = field(default_factory=lambda: [(0.0, 0.0)])

    def extend(self, body_xy: Point) -> bool:
        """Grow by one detection given in the agent's body frame. True if the hull changed."""
        if math.hypot(*body_xy) > self.max_range:
            return False
        if outside_distance(self.hull, body_xy) <= self.margin:
            return False
        hull = convex_hull([*self.hull, (float(body_xy[0]), float(body_xy[1]))])
        while len(hull) > self.max_vertices:
            n = len(hull)
            # Removing a vertex of a convex hull keeps it convex; drop the one adding the least area
            i = min(range(n), key=lambda k: abs(_cross(hull[k - 1], hull[k], hull[(k + 1) % n])))
            hull.pop(i)
        self.hull = hull
        return True

    def observe(self, pose: tuple[float, float, float], world_xy: Point) -> bool:
        """A detection at world_xy reported while the agent was at pose (x, y, theta)."""
        x, y, theta = pose
        dx, dy = world_xy[0] - x, world_xy[1] - y
        c, s = math.cos(theta), math.sin(theta)
        return self.extend((c * dx + s * dy, -s * dx + c * dy))

    def reset(self) -> None:
        self.hull = [(0.0, 0.0)]

    @property
    def points(self) -> list[list[float]]:
        """Body-frame vertices for the console, which draws them at the agent's pose."""
        return [[round(px, 3), round(py, 3)] for px, py in self.hull]
