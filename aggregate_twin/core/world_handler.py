from __future__ import annotations

from dataclasses import dataclass, field
from core_msgs.instance_aggregate.payloads import ObstacleObservation

@dataclass
class WorldConfig:
    """Static description of the world, loaded once -- not from a live sim."""

    width: float
    height: float
    origin_x: float = 0.0
    origin_y: float = 0.0
    resolution: float = 0.4
    static_obstacles: list[ObstacleObservation] = field(default_factory=list)

    @property
    def specs(self) -> tuple[float, float, float, float]:
        """(W, H, ox, oy) -- the exact tuple GlobalGridMap already expects."""
        return (self.width, self.height, self.origin_x, self.origin_y)

    @classmethod
    def from_yaml(cls, raw :  dict) -> "WorldConfig":

        obstacles = [
            ObstacleObservation(
                id=str(o["id"]),
                x=o["x"], y=o["y"],
                theta=o.get("theta", 0.0),
                radius=o.get("radius"),
                polygon=o.get("polygon"),
                is_dynamic=False,
            )
            for o in raw.get("static_obstacles", [])
        ]

        return cls(
            width=raw["width"],
            height=raw["height"],
            origin_x=raw.get("origin_x", 0.0),
            origin_y=raw.get("origin_y", 0.0),
            resolution=raw.get("resolution", 0.4),
            static_obstacles=obstacles,
        )
