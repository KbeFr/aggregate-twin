from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from core_msgs.instance_aggregate.payloads import ObstacleObservation

# ══════════════════════════════════════════════════════════════════════════
# World specification
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WorldSpec:
    """Metric extent of the world and its discretisation.

    All world <-> cell conversion goes through this object, so index
    conventions cannot drift between the cost map and the planner.
    """

    width: float                 # [m]
    height: float                # [m]
    resolution: float            # [m/cell]
    origin_x: float = 0.0        # [m] world coordinate of cell (0, 0)'s corner
    origin_y: float = 0.0        # [m]

    static_obstacles: list[ObstacleObservation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"world extent must be positive, got {self.width}x{self.height}")
        if self.resolution <= 0:
            raise ValueError(f"resolution must be positive, got {self.resolution}")
        if self.resolution > min(self.width, self.height) / 4:
            raise ValueError(
                f"resolution {self.resolution} m is too coarse for a "
                f"{self.width}x{self.height} m world"
            )

    @classmethod
    def from_tuple(cls, world_specs: Sequence[float], resolution: float) -> "WorldSpec":
        """Accepts the legacy ``(W, H, ox, oy)`` tuple."""
        w, h, ox, oy = world_specs
        return cls(width=float(w), height=float(h), resolution=float(resolution),
                   origin_x=float(ox), origin_y=float(oy))

    # -- shape ------------------------------------------------------------
    @property
    def nx(self) -> int:
        return max(1, round(self.width / self.resolution))

    @property
    def ny(self) -> int:
        return max(1, round(self.height / self.resolution))

    @property
    def shape(self) -> tuple[int, int]:
        return self.nx, self.ny

    @property
    def origin(self) -> tuple[float, float]:
        return self.origin_x, self.origin_y

    # -- conversion -------------------------------------------------------
    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """Cell containing (x, y). May be out of bounds — check in_bounds()."""
        return (
            int(math.floor((x - self.origin_x) / self.resolution)),
            int(math.floor((y - self.origin_y) / self.resolution)),
        )

    def cell_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        """Centre of cell (gx, gy). Inverse of world_to_cell up to res/2."""
        return (
            self.origin_x + (gx + 0.5) * self.resolution,
            self.origin_y + (gy + 0.5) * self.resolution,
        )

    def in_bounds(self, gx: int, gy: int) -> bool:
        return 0 <= gx < self.nx and 0 <= gy < self.ny


    @classmethod
    def from_yaml(cls, raw :  dict) -> WorldSpec:

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
