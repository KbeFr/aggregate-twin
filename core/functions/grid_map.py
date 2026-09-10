"""
grid_map.py — GlobalGridMap
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from matplotlib.path import Path

from scipy.ndimage import distance_transform_edt, label

logger = logging.getLogger(__name__)



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


# ══════════════════════════════════════════════════════════════════════════
# Obstacle description
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ObstacleShape:
    """Rasterisable obstacle — the aggregate's only obstacle representation.

    Built from :class:`ObstacleObservation` messages. A circle is described by
    ``radius``; a polygon by world-frame ``polygon`` vertices. When both are
    present the polygon wins.
    """

    id: str
    x: float
    y: float
    radius: float = 0.0
    polygon: tuple[tuple[float, float], ...] | None = None
    dynamic: bool = False

    @classmethod
    def coerce(cls, obs) -> "ObstacleShape | None":
        """Accepts an ObstacleShape, an ObstacleObservation, or anything with
        the same attribute names. Returns None if it has no usable footprint."""
        if isinstance(obs, cls):
            return obs
        poly = getattr(obs, "polygon", None)
        radius = float(getattr(obs, "radius", 0.0) or 0.0)
        if not poly and radius <= 0.0:
            return None
        dynamic = getattr(obs, "is_dynamic", None)
        if dynamic is None:
            vx = float(getattr(obs, "vx", 0.0) or 0.0)
            vy = float(getattr(obs, "vy", 0.0) or 0.0)
            dynamic = math.hypot(vx, vy) > 1e-6
        return cls(
            id=str(getattr(obs, "id", id(obs))),
            x=float(obs.x),
            y=float(obs.y),
            radius=radius,
            polygon=tuple(map(tuple, poly)) if poly else None,
            dynamic=bool(dynamic),
        )


# ══════════════════════════════════════════════════════════════════════════
# Grid map
# ══════════════════════════════════════════════════════════════════════════

class GlobalGridMap:
    """Occupancy grid, distance field and traversal cost for the aggregate twin.

    Parameters
    ----------
    world :
        A :class:`WorldSpec`, or the legacy ``(W, H, ox, oy)`` tuple in which
        case ``resolution`` must also be given.
    obstacles :
        Initial obstacle descriptions. Non-dynamic ones form the static layer.
    resolution :
        Only used when ``world`` is a legacy tuple.

    Notes
    -----
    Derived layers (occupancy, distance field, risk, regions, cost layers) are
    computed lazily and cached. Invalidation *rebinds* the cached attributes to
    ``None`` rather than mutating the arrays in place, so a planner running on
    another thread can bind them once and keep a consistent snapshot for the
    duration of a search.
    """

    # Uncertainty [m²] used by the Wu term
    UNCERTAINTY_COVERED = 0.02      # aerial position fix available
    UNCERTAINTY_UNCOVERED = 2.00    # dead-reckoning drift

    RISK_RADIUS = 1.5               # [m] soft proximity penalty reach

    OCC_FREE = 0.0
    OCC_BLOCKED = 100.0
    OCC_THRESHOLD = 50.0

    def __init__(
        self,
        world: WorldSpec | Sequence[float],
        obstacles: Iterable = (),
        resolution: float | None = None,
    ) -> None:
        if not isinstance(world, WorldSpec):
            if resolution is None:
                raise TypeError("resolution is required when world is a tuple")
            world = WorldSpec.from_tuple(world, resolution)
        self.world = world

        self._static: np.ndarray = self._blank()
        self._dynamic: np.ndarray = self._blank()
        self._obstacles: dict[str, ObstacleShape] = {}
        self._coverage: set[tuple[int, int]] = set()

        self._occupied: np.ndarray | None = None
        self._occ_float: np.ndarray | None = None
        self._dist: np.ndarray | None = None
        self._risk: np.ndarray | None = None
        self._regions: np.ndarray | None = None
        self._cost_cache: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}

        self.set_static_obstacles(obstacles)


    # ── geometry delegation ───────────────────────────────────────────────

    @property
    def res(self) -> float:
        return self.world.resolution

    @property
    def nx(self) -> int:
        return self.world.nx

    @property
    def ny(self) -> int:
        return self.world.ny

    @property
    def shape(self) -> tuple[int, int]:
        return self.world.shape

    @property
    def origin(self) -> tuple[float, float]:
        return self.world.origin

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        return self.world.world_to_cell(x, y)

    def cell_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        return self.world.cell_to_world(gx, gy)

    def in_bounds(self, gx: int, gy: int) -> bool:
        return self.world.in_bounds(gx, gy)

    # ── obstacle input ────────────────────────────────────────────────────

    def set_static_obstacles(self, obstacles: Iterable) -> None:
        """(Re)build the static layer. Also stamps the world boundary walls."""
        grid = self._blank()
        count = 0
        for raw in obstacles:
            shape = ObstacleShape.coerce(raw)
            if shape is None or shape.dynamic:
                continue
            self._obstacles[shape.id] = shape
            self._rasterise(grid, shape)
            count += 1
        grid[0, :] = grid[-1, :] = grid[:, 0] = grid[:, -1] = True
        self._static = grid
        self._invalidate()
        logger.debug("[GlobalGridMap] static layer: %d obstacle(s), %d blocked cell(s)",
                     count, int(grid.sum()))

    def update_perception(self, obstacles: Iterable) -> None:
        """Replace the perceived layer with every currently reported obstacle"""
        grid = self._blank()
        seen: dict[str, ObstacleShape] = {}
        for raw in obstacles:
            shape = ObstacleShape.coerce(raw)
            if shape is None:
                continue
            seen[shape.id] = shape
            self._rasterise(grid, shape)
        self._dynamic = grid
        self._obstacles.update(seen)
        self._invalidate()

    @property
    def obstacles(self) -> dict[str, ObstacleShape]:
        return dict(self._obstacles)

    # ── derived layers ────────────────────────────────────────────────────

    @property
    def occupied(self) -> np.ndarray:
        """Boolean (nx, ny) mask: True where the cell is blocked."""
        if self._occupied is None:
            self._occupied = self._static | self._dynamic
        return self._occupied

    @property
    def occupancy_grid(self) -> np.ndarray:
        """Float (nx, ny) grid of 0 / 100, for ir-sim compatible consumers."""
        if self._occ_float is None:
            self._occ_float = np.where(self.occupied, self.OCC_BLOCKED, self.OCC_FREE)
        return self._occ_float

    # Backwards-compatible alias.
    grid = occupancy_grid

    @property
    def distance_field(self) -> np.ndarray:
        """Metres from each cell to the nearest blocked cell (0 inside one).

        This is the single geometric primitive behind both the risk layer and
        the robot-fits test, so a robot's footprint is never baked into the
        occupancy grid and one map serves agents of any radius.
        """
        if self._dist is None:
            self._dist = distance_transform_edt(
                ~self.occupied, sampling=(self.res, self.res)
            ).astype(np.float32)
        return self._dist

    @property
    def risk(self) -> np.ndarray:
        """Soft proximity penalty in [0, 1], 1 at an obstacle, 0 beyond RISK_RADIUS."""
        if self._risk is None:
            self._risk = np.clip(1.0 - self.distance_field / self.RISK_RADIUS, 0.0, 1.0)
        return self._risk

    @property
    def regions(self) -> np.ndarray:
        """Connected-component labels of free space (0 = blocked)."""
        if self._regions is None:
            self._regions, n = label(~self.occupied)
            logger.debug("[GlobalGridMap] %d free-space region(s)", n)
        return self._regions

    def fits(self, gx: int, gy: int, radius: float) -> bool:
        """True if a disc of `radius` centred on this cell is collision-free."""
        return bool(self.distance_field[gx, gy] >= radius)

    def reachable(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        """True if both cells are free and in the same component. O(1)."""
        if not (self.in_bounds(*a) and self.in_bounds(*b)):
            return False
        ra, rb = self.regions[a], self.regions[b]
        return bool(ra != 0 and ra == rb)

    def clearance_along(self, path_xy: np.ndarray) -> float:
        """Tightest clearance [m] along a (2, N) world-metre path.

        Use it to decide whether an active path is still safe after the dynamic
        layer changed, without replanning.
        """
        arr = np.asarray(path_xy, dtype=float)
        if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] == 0:
            return 0.0
        gx = np.clip(np.floor((arr[0] - self.world.origin_x) / self.res).astype(int), 0, self.nx - 1)
        gy = np.clip(np.floor((arr[1] - self.world.origin_y) / self.res).astype(int), 0, self.ny - 1)
        return float(self.distance_field[gx, gy].min())

    # ── coverage ──────────────────────────────────────────────────────────

    @property
    def coverage(self) -> set[tuple[int, int]]:
        """Cells currently believed to be inside UAV camera coverage."""
        return self._coverage

    def mark_covered(self, cells: Iterable[tuple[int, int]]) -> None:
        """Replace the coverage set. Invalidates the cost layers."""
        new = {(int(a), int(b)) for a, b in cells}
        if new != self._coverage:
            self._coverage = new
            self._cost_cache.clear()

    def update_coverage(self, coverage_geometries: list) -> None:
        """TODO: derive coverage from UAV-reported obstacle observations.

        Until this is implemented the coverage set stays empty and every cell is
        charged UNCERTAINTY_UNCOVERED, which makes the Wu term a constant
        multiple of step distance (i.e. it only rescales Wd).
        """

    # ── cost ──────────────────────────────────────────────────────────────

    def cost_layers(self, weights: tuple) -> tuple[np.ndarray, np.ndarray]:
        """Vectorised form of :meth:`cell_cost`, cached per weight tuple.

        Returns ``(per_metre, per_cell)`` such that for a free cell::

            cell_cost(gx, gy, step) == per_metre[gx, gy] * step + per_cell[gx, gy]

        The planner binds these two arrays once per search instead of calling
        cell_cost per expansion.
        """
        key = tuple(float(w) for w in weights)
        cached = self._cost_cache.get(key)
        if cached is not None:
            return cached

        Wd, _We, _Wt, Wu, Wr = key
        uncertainty = np.full(self.shape, self.UNCERTAINTY_UNCOVERED, dtype=np.float32)
        if self._coverage:
            idx = np.array(sorted(self._coverage), dtype=int)
            uncertainty[idx[:, 0], idx[:, 1]] = self.UNCERTAINTY_COVERED

        per_metre = (Wd + Wu * uncertainty).astype(np.float32)
        per_cell = (Wr * self.risk).astype(np.float32)
        self._cost_cache[key] = (per_metre, per_cell)
        return per_metre, per_cell

    def cell_cost(self, gx: int, gy: int, step_dist: float, weights: tuple) -> float:
        """Traversal cost of entering one cell, or inf if it is blocked.

        Only map-side terms: distance (Wd), uncertainty (Wu), risk (Wr).
        Energy and time depend on the agent and are bid by the instance twin.
        """
        if not self.in_bounds(gx, gy) or self.occupied[gx, gy]:
            return math.inf
        per_metre, per_cell = self.cost_layers(weights)
        return float(per_metre[gx, gy] * step_dist + per_cell[gx, gy])

    def min_cost_per_meter(self, weights: tuple) -> float:
        """Cheapest achievable cost of one metre under `weights`.

        Lower bound for the A* heuristic. It must track :meth:`cell_cost`
        exactly or the search stops being admissible: risk >= 0 and uncertainty
        is at best UNCERTAINTY_COVERED — and only if any cell is covered at all.
        """
        Wd, _We, _Wt, Wu, _Wr = weights
        best_uncertainty = (
            self.UNCERTAINTY_COVERED if self._coverage else self.UNCERTAINTY_UNCOVERED
        )
        return float(Wd + Wu * best_uncertainty)

    def get_cost_image(self, weights: tuple) -> np.ndarray:
        """(nx, ny) cost field normalised to [0, 1] for visualisation.

        Blocked cells are 1.0. Caller transposes to (ny, nx) for imshow.
        """
        per_metre, per_cell = self.cost_layers(weights)
        img = per_metre * (self.res * math.sqrt(2.0)) + per_cell
        img = img.astype(np.float64)

        free = ~self.occupied
        if free.any():
            lo, hi = img[free].min(), img[free].max()
            if hi > lo:
                img = (img - lo) / (hi - lo)
        img[self.occupied] = 1.0
        return np.clip(img, 0.0, 1.0)

    # ── rasterisation ─────────────────────────────────────────────────────

    def _blank(self) -> np.ndarray:
        return np.zeros(self.world.shape, dtype=bool)

    def _invalidate(self) -> None:
        """Drop derived layers. Rebinds rather than mutating (thread safety)."""
        self._occupied = None
        self._occ_float = None
        self._dist = None
        self._risk = None
        self._regions = None
        self._cost_cache = {}
        env_map = getattr(self, "env_map", None)
        if env_map is not None:
            env_map.grid = self.occupancy_grid

    def _rasterise(self, grid: np.ndarray, shape: ObstacleShape) -> None:
        if shape.polygon and len(shape.polygon) >= 3:
            self._rasterise_polygon(grid, shape.polygon)
        elif shape.radius > 0.0:
            self._rasterise_circle(grid, shape.x, shape.y, shape.radius)

    def _bbox(self, minx: float, miny: float, maxx: float, maxy: float,
              pad_cells: int = 1) -> tuple[int, int, int, int] | None:
        ox, oy = self.world.origin
        i0 = max(0, int(math.floor((minx - ox) / self.res)) - pad_cells)
        i1 = min(self.nx - 1, int(math.ceil((maxx - ox) / self.res)) + pad_cells)
        j0 = max(0, int(math.floor((miny - oy) / self.res)) - pad_cells)
        j1 = min(self.ny - 1, int(math.ceil((maxy - oy) / self.res)) + pad_cells)
        if i1 < i0 or j1 < j0:
            return None
        return i0, i1, j0, j1

    def _cell_centres(self, i0: int, i1: int, j0: int, j1: int):
        ox, oy = self.world.origin
        xs = ox + (np.arange(i0, i1 + 1) + 0.5) * self.res
        ys = oy + (np.arange(j0, j1 + 1) + 0.5) * self.res
        return xs, ys

    def _rasterise_circle(self, grid: np.ndarray, cx: float, cy: float, r: float) -> None:
        # Half a cell of padding keeps the rasterisation conservative: a cell is
        # blocked as soon as the disc touches any part of it.
        pad = r + self.res * 0.5
        box = self._bbox(cx - pad, cy - pad, cx + pad, cy + pad, pad_cells=0)
        if box is None:
            return
        i0, i1, j0, j1 = box
        xs, ys = self._cell_centres(i0, i1, j0, j1)
        d2 = (xs[:, None] - cx) ** 2 + (ys[None, :] - cy) ** 2
        grid[i0:i1 + 1, j0:j1 + 1] |= d2 <= pad * pad

    def _rasterise_polygon(self, grid: np.ndarray, polygon: Sequence[Sequence[float]]) -> None:
        pts = np.asarray(polygon, dtype=float)
        box = self._bbox(pts[:, 0].min(), pts[:, 1].min(),
                         pts[:, 0].max(), pts[:, 1].max(), pad_cells=1)
        if box is None:
            return
        i0, i1, j0, j1 = box
        xs, ys = self._cell_centres(i0, i1, j0, j1)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        inside = Path(pts, closed=True).contains_points(
            np.column_stack([gx.ravel(), gy.ravel()])
        ).reshape(gx.shape)
        # One-cell dilation instead of Path(radius=...): winding-order
        # independent, and guarantees we never under-approximate.
        grid[i0:i1 + 1, j0:j1 + 1] |= _dilate1(inside)


def _dilate1(mask: np.ndarray) -> np.ndarray:
    """8-connected one-cell dilation, no scipy round-trip for small patches."""
    out = mask.copy()
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            src = mask
            if di == 1:
                src = np.pad(src, ((1, 0), (0, 0)))[:-1, :]
            elif di == -1:
                src = np.pad(src, ((0, 1), (0, 0)))[1:, :]
            if dj == 1:
                src = np.pad(src, ((0, 0), (1, 0)))[:, :-1]
            elif dj == -1:
                src = np.pad(src, ((0, 0), (0, 1)))[:, 1:]
            out |= src
    return out