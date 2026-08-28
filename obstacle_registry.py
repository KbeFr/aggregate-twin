# obstacle_registry.py
from dataclasses import dataclass
import math

from core_msgs.instance_aggregate.payloads import ObstacleObservation


@dataclass
class ObstacleReport:
    obs: "ObstacleObservation"
    reporter: str          # agent name that saw it
    received: float        # sim time

    @property
    def footprint(self) -> float:
        """Rough radius [m] covering the report, polygon or circle."""
        if self.obs.polygon:
            return max(math.hypot(px - self.obs.x, py - self.obs.y)
                       for px, py in self.obs.polygon)
        return float(self.obs.radius or 0.0)


class ObstacleRegistry:
    """Every obstacle report, kept per reporting agent."""

    def __init__(self, max_age: float = 2.0) -> None:
        self.max_age = max_age
        self._reports: dict[str, dict[str, ObstacleReport]] = {}   # reporter -> id -> report

    def ingest(self, reporter: str, obs, now: float) -> None:
        self._reports.setdefault(reporter, {})[str(obs.id)] = ObstacleReport(obs, reporter, now)

    def prune(self, now: float) -> None:
        """Drop stale reports, or a moving obstacle smears across the map forever."""
        for reporter, per_id in self._reports.items():
            for oid in [k for k, r in per_id.items()
                        if r.obs.is_dynamic and now - r.received > self.max_age]:
                per_id.pop(oid)

    def drop_reporter(self, reporter: str) -> None:
        self._reports.pop(reporter, None)

    # -- views ---------------------------------------------------------
    def by_reporter(self) -> dict[str, list[ObstacleReport]]:
        return {r: list(d.values()) for r, d in self._reports.items()}

    def all_reports(self) -> list[ObstacleReport]:
        return [r for d in self._reports.values() for r in d.values()]

    def observations(self) -> list:
        """Flat list for GlobalGridMap.update_perception()."""
        return [r.obs for r in self.all_reports()]

    def clusters(self, tol: float = 0.0) -> list[list[ObstacleReport]]:
        """Group reports whose footprints overlap — i.e. probably the same
        physical obstacle. Groups with >1 reporter are corroborated;
        single-reporter groups are unconfirmed."""
        reports = self.all_reports()
        groups: list[list[ObstacleReport]] = []
        for rep in reports:
            for g in groups:
                if any(self._overlaps(rep, other, tol) for other in g):
                    g.append(rep)
                    break
            else:
                groups.append([rep])
        return groups

    @staticmethod
    def _overlaps(a: ObstacleReport, b: ObstacleReport, tol: float) -> bool:
        d = math.hypot(a.obs.x - b.obs.x, a.obs.y - b.obs.y)
        return d <= a.footprint + b.footprint + tol

    def disagreements(self, tol: float = 0.0) -> list[list[ObstacleReport]]:
        """Corroborated groups whose members disagree about where/how big it is.
        Feed this to detect_perception_faults()."""
        out = []
        for g in self.clusters(tol):
            if len({r.reporter for r in g}) < 2:
                continue
            spread = max(math.hypot(a.obs.x - b.obs.x, a.obs.y - b.obs.y)
                         for a in g for b in g)
            if spread > min(r.footprint for r in g):
                out.append(g)
        return out