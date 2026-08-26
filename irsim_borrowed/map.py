from __future__ import annotations

import warnings

import numpy as np
import shapely
from shapely import Point


class Map:
    """Map data container for navigation / path-planning.

    Extracted from the Irsim project:
        R. Han, “Ir-sim: An open-source lightweight simulator for robot
        navigation, control, and learning,” 2026, accessed: 2026-05-20.
        [Online]. Available: https://github.com/hanruihua/ir-sim



    Satisfies the :class:`EnvGridMap` protocol so that it can be passed to
    any planner that expects ``EnvGridMap``.

    Collision precedence (shared by all planners):
      1. Grid lookup (O(1) per cell) when *grid* is not ``None``.
      2. Shapely geometry intersection when *grid* is unavailable.
    """

    def __init__(
        self,
        width: float = 10,
        height: float = 10,
        resolution: float = 0.1,
        obstacle_list: list | None = None,
        grid: np.ndarray | None = None,
        world_offset: tuple[float, float] | list[float] | None = None,
    ):
        """
        Initialize the Map.

        Args:
            width: Width of the world (metres).
            height: Height of the world (metres).
            resolution: Planner discretisation cell size (metres/cell).
            obstacle_list: Obstacle objects for Shapely collision detection.
            grid: Occupancy grid (0-100) for grid-based collision detection.
            world_offset: World origin (x, y) for grid indexing. When non-zero,
                geometry and positions are interpreted in world coordinates so
                grid lookups align with ObstacleMap. Default (0, 0).
        """
        if obstacle_list is None:
            obstacle_list = []
        self.width = width
        self.height = height
        self.resolution = resolution
        self.obstacle_list = obstacle_list
        self.grid = grid
        if world_offset is None:
            world_offset = (0.0, 0.0)
        self.world_offset = (float(world_offset[0]), float(world_offset[1]))
        self._obstacles_prepared: bool = False

        # Warn when the user-specified resolution diverges from the actual
        # grid cell size by more than 5 %.
        if grid is not None:
            gr = self.grid_resolution
            if gr is not None:
                gx, _gy = gr
                if abs(resolution - gx) / max(resolution, gx) > 0.05:
                    warnings.warn(
                        f"Map.resolution ({resolution}) differs from grid "
                        f"cell size ({gx:.4f} x {_gy:.4f}). Grid-based "
                        f"planners will use grid_resolution for lookups.",
                        stacklevel=2,
                    )

    @property
    def grid_resolution(self) -> tuple[float, float] | None:
        """Actual cell size ``(x_reso, y_reso)`` derived from *grid* shape and world size.

        Returns ``None`` when no grid is present.
        """
        if self.grid is None:
            return None
        return (
            self.width / self.grid.shape[0],
            self.height / self.grid.shape[1],
        )

    def grid_occupied(
        self,
        x: float,
        y: float,
        margin_x: float = 0.0,
        margin_y: float = 0.0,
        threshold: float = 50.0,
    ) -> bool | None:
        """Check if any grid cell within the bounding box around ``(x, y)`` is occupied.

        The bounding box extends *margin_x* / *margin_y* (in world metres) in
        each direction.  Grid cells whose occupancy exceeds *threshold* are
        considered occupied.

        Returns:
            ``None`` when no grid is present (caller should fall back to
            Shapely or another collision method).  ``True`` / ``False``
            otherwise.  Points outside the world bounds are treated as
            occupied so planners cannot escape the map.
        """
        if self.grid is None:
            return None
        gr = self.grid_resolution
        if gr is None:
            return None  # defensive; grid is None already caught above
        ox, oy = self.world_offset
        if x < ox or x >= ox + self.width or y < oy or y >= oy + self.height:
            return True  # out-of-bounds: treat as occupied
        rx, ry = gr
        gx = int((x - ox) / rx)
        gy = int((y - oy) / ry)
        rows, cols = self.grid.shape
        mx = max(1, int(np.ceil(margin_x / rx))) if margin_x > 0 else 0
        my = max(1, int(np.ceil(margin_y / ry))) if margin_y > 0 else 0
        return bool(
            np.any(
                self.grid[
                    max(0, gx - mx) : min(rows, gx + mx + 1),
                    max(0, gy - my) : min(cols, gy + my + 1),
                ]
                > threshold
            )
        )

    def is_collision(self, geometry) -> bool:
        """Check collision for a Shapely geometry against grid + obstacles.

        Collision precedence:
          1. Grid lookup when *grid* is not None; if occupied, collision.
          2. When the grid reports free or is unavailable, Shapely geometry
             intersection with *obstacle_list*.
        """
        minx, miny, maxx, maxy = geometry.bounds
        ox, oy = self.world_offset
        if (
            minx < ox
            or miny < oy
            or maxx >= ox + self.width
            or maxy >= oy + self.height
        ):
            return True
        if self.grid is not None:
            gr = self.grid_resolution
            if gr is not None and _grid_collision_geometry(
                self.grid, gr, geometry, world_offset=self.world_offset
            ):
                return True
        if not self.obstacle_list:
            return False
        if not self._obstacles_prepared:
            for obj in self.obstacle_list:
                shapely.prepare(obj._geometry)
            self._obstacles_prepared = True
        return any(
            shapely.intersects(geometry, obj._geometry) for obj in self.obstacle_list
        )


def _grid_collision_geometry(
    grid: np.ndarray,
    grid_reso: tuple[float, float],
    geometry,
    world_offset: tuple[float, float] = (0.0, 0.0),
) -> bool:
    """
    Extracted from the Irsim project:
        R. Han, “Ir-sim: An open-source lightweight simulator for robot
        navigation, control, and learning,” 2026, accessed: 2026-05-20.
        [Online]. Available: https://github.com/hanruihua/ir-sim



    Check collision of a Shapely geometry against an occupancy grid.

    Uses the same logic as ObstacleMap.check_grid_collision.
    """
    if grid is None:
        return False

    minx, miny, maxx, maxy = geometry.bounds
    x_reso, y_reso = grid_reso
    offset_x, offset_y = world_offset

    i_min = max(0, int((minx - offset_x) / x_reso))
    i_max = min(grid.shape[0] - 1, int((maxx - offset_x) / x_reso))
    j_min = max(0, int((miny - offset_y) / y_reso))
    j_max = min(grid.shape[1] - 1, int((maxy - offset_y) / y_reso))

    if i_min > i_max or j_min > j_max:
        return False

    collision_radius = max(x_reso, y_reso) * COLLISION_RADIUS_FACTOR

    for i in range(i_min, i_max + 1):
        for j in range(j_min, j_max + 1):
            if grid[i, j] > OCCUPANCY_THRESHOLD:
                cell_x = offset_x + (i + CELL_CENTER_OFFSET) * x_reso
                cell_y = offset_y + (j + CELL_CENTER_OFFSET) * y_reso
                cell_center = Point(cell_x, cell_y)
                if geometry.distance(cell_center) <= collision_radius:
                    return True

    return False
