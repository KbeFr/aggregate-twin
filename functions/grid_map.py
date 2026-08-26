"""
cost_map.py  —  GlobalGridMap
==============================
Builds the static occupancy grid from ir-sim obstacle geometry and computes
per-cell traversal cost for the A* planner.
"""

from __future__ import annotations

import math
import time
import numpy as np
from matplotlib.path import Path
from shapely.geometry import Point

from irsim_borrowed.map import Map


class GlobalGridMap:
    """
    Static occupancy grid + per-cell traversal cost map.

    One instance is shared across all UGVs.  Per-robot variables (mass,
    speed, payload) are passed as arguments to cell_cost()

    Parameters
    ----------
    world_specs : tuple (W, H, ox, oy)
        World width, height, and x/y origin offset in metres.
    obstacle_list : list
        ir-sim obstacle objects (static + dynamic mixed; dynamic ones are
        filtered out automatically by _is_dynamic()).
    resolution : float
        Grid cell edge length in metres.  0.3-0.5 m is sensible for a 40 m world.
    """

    # --- Uncertainty constants
    UNCERTAINTY_COVERED   = 0.02   # m²  — aerial position fix available
    UNCERTAINTY_UNCOVERED = 2.00   # m²  — dead-reckoning drift

    # --- Risk inflation radius (for around objects)
    RISK_RADIUS = 1.5              # m

    def __init__(
        self,
        world_specs:   tuple,
        obstacle_list: list,
        resolution:    float,
    ) -> None:

        self.res = resolution

        self._W, self._H, self._ox, self._oy = world_specs
        self.obstacle_list = obstacle_list

        self._occ: np.ndarray = self._create_occupancy_grid()

        self.nx, self.ny = self._occ.shape
        self._risk: np.ndarray = self._build_risk_layer()
        self._coverage: set[tuple[int, int]] = set()

        self.env_map = Map(
            width=self._W,
            height=self._H,
            resolution=self.res,
            obstacle_list=self.obstacle_list,
            grid=self._occ,
            world_offset=(self._ox, self._oy)
        )


    # --- Properties

    @property
    def grid(self) -> np.ndarray:
        """Occupancy grid (nx x ny), values 0 (free) or 100 (obstacle)."""
        return self._occ

    @property
    def coverage(self) -> set[tuple[int, int]]:
        """Set of (gx, gy) cells currently inside UAV camera footprint."""
        return self._coverage


    # --- Coverage update

    def update_coverage(self, coverage_geometries: list) -> None:
        """
        Rebuild UAV coverage set from Shapely camera footprints.
        Uses a vectorised Matplotlib Path check, fast for large grids.
        """

        new_coverage: set[tuple[int, int]] = set()

        for geom in coverage_geometries:
            if geom is None or geom.is_empty:
                continue

            minx, miny, maxx, maxy = geom.bounds

            i0 = max(0,          int((minx - self._ox) / self.res))
            i1 = min(self.nx-1,  int((maxx - self._ox) / self.res) + 1)
            j0 = max(0,          int((miny - self._oy) / self.res))
            j1 = min(self.ny-1,  int((maxy - self._oy) / self.res) + 1)

            if i0 > i1 or j0 > j1:
                continue

            vertices = np.array(geom.exterior.coords)

            ii, jj = np.meshgrid(
                np.arange(i0, i1 + 1),
                np.arange(j0, j1 + 1),
                indexing='ij',
            )

            cx = self._ox + (ii + 0.5) * self.res
            cy = self._oy + (jj + 0.5) * self.res

            points = np.c_[cx.ravel(), cy.ravel()]
            mask   = Path(vertices).contains_points(points)

            new_coverage.update(
                zip(ii.ravel()[mask].tolist(), jj.ravel()[mask].tolist(), strict=False)
            )
        self._coverage = new_coverage

    # --- Cost function

    def cell_cost(
        self,
        gx:         int,
        gy:         int,
        step_dist:  float,
        weights:    tuple,
        robot_mass: float,
        v_avg:      float = 0.4,
        Ka:         float = 0.5,
        Ku:         float = 0.05,
    ) -> float:
        """
        Compute the full traversal cost for one grid cell.

        Parameters
        ----------
        gx, gy      : grid cell indices.
        step_dist   : arc-length of the motion step in metres
                      (resolution for cardinal, resolution*√2 for diagonal).
        weights     : (Wd, We, Wt, Wu, Wr) scalar weights.
        robot_mass  : total robot + payload mass in kg.
        v_avg       : average traversal speed [m/s].
        Ka          : ancillary power constant [W] (sensing, comms, compute).
        Ku          : rolling friction coefficient [dimensionless].

        Returns
        -------
        float : total cell cost, or math.inf if the cell is occupied.
        """
        Wd, We, Wt, Wu, Wr = weights

        if self._occ[gx, gy] > 50:
            return math.inf

        g  = 9.81
        dt = step_dist / max(v_avg, 1e-6)

        # Distance term
        cost = Wd * step_dist

        # Energy term  E = 2·Ku·m·g·d  +  Ka·dt
        cost += We * (2.0 * Ku * robot_mass * g * step_dist + Ka * dt)

        # Time term
        cost += Wt * dt

        # Risk term (soft proximity penalty)
        cost += Wr * self._risk[gx, gy]

        # Uncertainty term — the thesis core variable
        uncertainty = (
            self.UNCERTAINTY_COVERED
            if (gx, gy) in self._coverage
            else self.UNCERTAINTY_UNCOVERED
        )
        cost += Wu * uncertainty * step_dist

        return cost

    def get_cost_image(
        self,
        weights:    tuple,
        robot_mass: float,
        v_avg:      float = 0.4,
        Ka:         float = 0.5,
        Ku:         float = 0.05,
    ) -> np.ndarray:
        """
        Return a (nx, ny) normalized [0,1] cost array for visualization.
        Obstacle cells are NaN.  Caller transposes to (ny, nx) for imshow.

        Parameters
        ----------
        :param robot_mass: robot + payload mass in kg.
        :param weights: (Wd, We, Wt, Wu, Wr).
        :param Ku: Robot friction coefficient [dimensionless].
        :param Ka: Robot ancillary power constant [dimensionless].
        :param v_avg: Robot average traversal speed [m/s].
        """
        diag = self.res * math.sqrt(2)

        img = np.array(
            [
                [self.cell_cost(i, j, diag, weights, robot_mass , v_avg=v_avg , Ka=Ka , Ku=Ku) for j in range(self.ny)]
                for i in range(self.nx)
            ],
            dtype=np.float64,
        )

        img[np.isinf(img)] = np.nan
        finite = img[np.isfinite(img)]
        if finite.size > 0:
            lo, hi = finite.min(), finite.max()
            if hi > lo:
                img = (img - lo) / (hi - lo)

        img[np.isnan(img)] = 1.0

        return img


    def update_perception(self, perceived_obstacles: list) -> None:
            """
            Update the map based on newly perceived obstacles.
            """
            # update the obstacle list
            self.obstacle_list = perceived_obstacles


            # TODO optimize this
            # Re-rasterize grid and risk layers
            self._occ = self._create_occupancy_grid()
            self._risk = self._build_risk_layer()

            # Update the ir-sim Map object
            self.env_map.obstacle_list = self.obstacle_list
            self.env_map.grid = self._occ

    # --- Coordinate helpers

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        gx = int(np.clip((x - self._ox) / self.res, 0, self.nx - 1))
        gy = int(np.clip((y - self._oy) / self.res, 0, self.ny - 1))
        return gx, gy

    def cell_to_world(self, gx: int, gy: int) -> tuple[float, float]:
        return (
            self._ox + (gx + 0.5) * self.res,
            self._oy + (gy + 0.5) * self.res,
        )


    # --- Private helpers

    def _create_occupancy_grid(self) -> np.ndarray:
        """
        Convert Shapely obstacle geometries to a discrete occupancy grid.
        Dynamic obstacles are excluded.
        Robot-radius clearance is added via isotropic binary dilation.
        """

        #Time the rasterization for possible optimization
        print("[GlobalGridMap] Rasterising static obstacles…")
        t0 = time.perf_counter()

        nx = round(self._W / self.res)
        ny = round(self._H / self.res)
        grid = np.zeros((nx, ny), dtype=np.float64)

        for obs in self.obstacle_list:
            if not getattr(obs, '_geometry_valid', False):
                continue
            if getattr(obs, 'unobstructed', False):
                continue
            if self._is_dynamic(obs):
                continue
            geom = obs._geometry
            if geom is None:
                continue

            minx, miny, maxx, maxy = geom.bounds
            i0 = max(0,    int((minx - self._ox) / self.res) - 1)
            i1 = min(nx-1, int((maxx - self._ox) / self.res) + 1)
            j0 = max(0,    int((miny - self._oy) / self.res) - 1)
            j1 = min(ny-1, int((maxy - self._oy) / self.res) + 1)

            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    cx = self._ox + (i + 0.5) * self.res
                    cy = self._oy + (j + 0.5) * self.res
                    if geom.distance(Point(cx, cy)) <= self.res * 0.5:
                        grid[i, j] = 100.0

        # Robot-radius clearance (isotropic binary dilation)
        robot_r   = 0.22
        n_inflate = math.ceil(robot_r / self.res)
        if n_inflate > 0:
            try:
                from scipy.ndimage import binary_dilation
                struct   = np.ones((2 * n_inflate + 1, 2 * n_inflate + 1), dtype=bool)
                inflated = binary_dilation(grid > 50, structure=struct)
                grid[inflated] = 100.0
            except ImportError:
                pass

        # Hard world-boundary walls
        grid[0, :] = grid[-1, :] = grid[:, 0] = grid[:, -1] = 100.0

        print(
            f"[GlobalGridMap] Grid {grid.size}  "
            f"{int(np.sum(grid > 50))} obstacle cells  "
            f"{(time.perf_counter() - t0) * 1000:.0f} ms"
        )

        return grid

    def _build_risk_layer(self) -> np.ndarray:
        """
        Gradient soft-cost around obstacle cells.  Built once after _rasterise.
        """
        risk  = np.zeros((self.nx, self.ny), dtype=np.float64)
        inf_n = math.ceil(self.RISK_RADIUS / self.res)

        for ox, oy in np.argwhere(self._occ > 50):
            for i in range(max(0, ox - inf_n), min(self.nx, ox + inf_n + 1)):
                for j in range(max(0, oy - inf_n), min(self.ny, oy + inf_n + 1)):
                    dist = math.hypot(i - ox, j - oy) * self.res
                    if dist <= self.RISK_RADIUS:
                        r = max(0.0, 1.0 - dist / self.RISK_RADIUS)
                        risk[i, j] = max(risk[i, j], r)
        return risk

    def _is_dynamic(self, obs) -> bool:
        """True if obs moves during the simulation (exclude from static grid)."""
        if getattr(obs, 'static', True):
            return False
        if getattr(obs, 'behavior', None) is not None:
            return True
        vel_max = getattr(obs, 'vel_max', None)
        return bool(vel_max is not None and np.any(np.abs(np.array(vel_max).flatten()) > 1e-06))

    @property
    def occupancy_grid(self):
        return self._occ


