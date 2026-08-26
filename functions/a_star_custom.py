"""
A* grid planning.

Collision precedence:
  1. Grid lookup when ``env_map.grid`` is not ``None``; if occupied, collision.
  2. When the grid reports free or is unavailable, Shapely vs. obstacle_list.
  (Grid and obstacle_list are combined when both are present.)

author: Atsushi Sakai(@Atsushi_twi)
        Nikos Kanargias (nkana@tee.gr)

adapted by: Reinis Cimurs

further customized for project specific use by: Kobe Frateur

See Wikipedia article (https://en.wikipedia.org/wiki/A*_search_algorithm)
"""

from __future__ import annotations

import contextlib
import heapq
import logging
import math

import numpy as np

from functions.grid_map import GlobalGridMap

from irsim_twin.lib.handler.geometry_handler import GeometryFactory
from base.irsim_borrowed.util import to_numpy
from irsim_borrowed.map import Map

logger = logging.getLogger(__name__)


class AStarPlannerCustom:
    def __init__(self, map : Map) -> None:
        """
        Initialize A* planner.

        Args:
            map : live map instance of whole environment used in aggregate twin instance
        """

        self.obstacle_list = map.obstacle_list
        off = np.asarray(map.world_offset, dtype=float).flatten()
        self.origin_x = float(off[0])
        self.origin_y = float(off[1])
        self.min_x, self.min_y = 0, 0  # grid indices are 0-based
        self.max_x = self.origin_x + map.width
        self.max_y = self.origin_y + map.height
        # When map has a grid, use its actual resolution and shape so planner grid
        # matches collision lookups (avoids "Open set is empty" on resolution mismatch).
        grid = getattr(map, "grid", None)
        gr = None
        if grid is not None and hasattr(map, "grid_resolution"):
            with contextlib.suppress(Exception):
                gr = map.grid_resolution
        if grid is not None and gr is not None:
            self.resolution = gr[0]  # m/cell; assume square cells (gr[0]==gr[1])
            self.x_width = grid.shape[0]
            self.y_width = grid.shape[1]
        else:
            self.resolution = map.resolution
            self.x_width = round((self.max_x - self.origin_x) / self.resolution)
            self.y_width = round((self.max_y - self.origin_y) / self.resolution)
        self.motion = self.get_motion_model()

    class Node:
        """Node class"""

        def __init__(self, x: int, y: int, cost: float, parent_index: int) -> None:
            """
            Initialize Node

            Args:
                x (float): x position of the node
                y (float): y position of the node
                cost (float): heuristic cost of the node
                parent_index (int): Nodes parent index
            """

            self.x = x  # index of grid
            self.y = y  # index of grid
            self.cost = cost
            self.parent_index = parent_index

        def __str__(self) -> str:
            """str function for Node class"""
            return (
                    str(self.x)
                    + ","
                    + str(self.y)
                    + ","
                    + str(self.cost)
                    + ","
                    + str(self.parent_index)
            )

    def planning(
            self,
            start_pose: np.ndarray,
            goal_pose: np.ndarray,
            weights,
            global_grid_map: GlobalGridMap,
    ) -> tuple[list[float], list[float], float]:
        """
        A star path search

        Args:
            global_grid_map: the grid map object
            weights: mission weights for planning
            start_pose (np.array): start pose [x,y]
            goal_pose (np.array): goal pose [x,y]

        Returns:
            (np.array): xy position array of the final path
        """
        start_node = self.Node(
            self.calc_xy_index(float(to_numpy(start_pose)[0].item()), self.origin_x),
            self.calc_xy_index(float(to_numpy(start_pose)[1].item()), self.origin_y),
            0.0,
            -1,
        )
        goal_node = self.Node(
            self.calc_xy_index(float(to_numpy(goal_pose)[0].item()), self.origin_x),
            self.calc_xy_index(float(to_numpy(goal_pose)[1].item()), self.origin_y),
            0.0,
            -1,
        )

        robot_mass = getattr(ugv, 'mass', 1.0)
        robot_avg_speed = getattr(ugv, 'avg_speed', 1.0)
        robot_anc_drain = getattr(ugv, 'ancillary_drain', 0)
        robot_friction = getattr(ugv, 'friction', 0.2)

        print("[A_STAR] Robots mass : " + str(robot_mass))
        print("[A_STAR] Robots avg_speed : " + str(robot_avg_speed))
        print("[A_STAR] Robots anc_drain : " + str(robot_anc_drain))
        print("[A_STAR] Robots friction : " + str(robot_friction))

        # Occupancy grid for obstacle checking
        occ = global_grid_map.occupancy_grid

        open_set, closed_set = {}, {}
        start_n_id = self.calc_grid_index(start_node)
        open_set[start_n_id] = start_node

        # Set up the Priority Queue (Heap)
        pq = []
        start_f_cost = start_node.cost + self.calc_heuristic(start_node, goal_node, weights, robot_mass, robot_avg_speed, robot_anc_drain, robot_friction)
        heapq.heappush(pq, (start_f_cost, start_n_id))

        while pq:
            #  retrieval of lowest cost node
            _current_f, c_id = heapq.heappop(pq)

            if c_id in closed_set:
                continue

            if c_id not in open_set:
                continue

            current = open_set[c_id]


            if current.x == goal_node.x and current.y == goal_node.y:
                print("Find goal")
                goal_node.parent_index = current.parent_index
                goal_node.cost = current.cost
                break

            # Remove the item from the open set
            del open_set[c_id]

            # Add it to the closed set
            closed_set[c_id] = current

            # expand_grid search grid based on motion model
            for i, _ in enumerate(self.motion):

                # Calculate the target cell coordinates
                nx = current.x + self.motion[i][0]
                ny = current.y + self.motion[i][1]

                # fast bound checking
                if nx < 0 or ny < 0 or nx >= self.x_width or ny >= self.y_width:
                    continue

                # occupancy grid check
                if occ[nx,ny] > 50:
                    continue

                # Convert the grid step (1 or 1.414) into physical meters
                step_dist = self.motion[i][2] * self.resolution

                # calc cost
                move_cost = global_grid_map.cell_cost(
                    gx=nx,
                    gy=ny,
                    step_dist=step_dist,
                    weights=weights,
                    robot_mass=robot_mass,
                    v_avg=robot_avg_speed,
                    Ka=robot_anc_drain,
                    Ku=robot_friction,
                )

                if math.isinf(move_cost):
                    continue

                # Create the valid neighbor node using the cumulative cost
                node = self.Node(
                    nx,
                    ny,
                    current.cost + move_cost,
                    c_id,
                )
                n_id = self.calc_grid_index(node)

                if n_id in closed_set:
                    continue

                if n_id not in open_set or open_set[n_id].cost > node.cost:
                    open_set[n_id] = node  # discovered a new node or found better path

                    # Calculate new F-cost and push to heap
                    f_cost = node.cost + self.calc_heuristic(node, goal_node, weights, robot_mass, robot_avg_speed, robot_anc_drain, robot_friction)
                    heapq.heappush(pq, (f_cost, n_id))

        rx, ry, global_cost = self.calc_final_path(goal_node, closed_set)

        return np.array([rx, ry]), global_cost

    def calc_final_path(
            self, goal_node: Node, closed_set: dict
    ) -> tuple[list[float], list[float], float]:
        """Generate the final path

        Args:
            goal_node (Node): final goal node
            closed_set (dict): dict of closed nodes

        Returns:
            rx (list): list of x positions of final path
            ry (list): list of y positions of final path
            total_cost (float): exact final cumulative path cost
        """
        rx, ry = (
            [self.calc_grid_position(goal_node.x, self.origin_x)],
            [self.calc_grid_position(goal_node.y, self.origin_y)],
        )
        total_cost = goal_node.cost
        parent_index = goal_node.parent_index
        while parent_index != -1:
            n = closed_set[parent_index]
            rx.append(self.calc_grid_position(n.x, self.origin_x))
            ry.append(self.calc_grid_position(n.y, self.origin_y))
            parent_index = n.parent_index

        return rx, ry, total_cost

    def calc_heuristic(
            self,
            n1: Node,
            n2: Node,
            weights: tuple,
            robot_mass: float,
            v_avg: float,
            Ka: float,
            Ku: float
    ) -> float:
        """
        Admissible optimal heuristic tailored to multi-objective cost map.
        Calculates absolute theoretical minimum cost per meter (c_min) to maintain
        mathematical perfection while boosting search directionality.
        """
        # Physical distance in meters
        distance = math.hypot(n1.x - n2.x, n1.y - n2.y) * self.resolution

        Wd, We, Wt, Wu,_Wr = weights

        g = 9.81
        v = max(v_avg, 1e-6)

        # Minimum potential traversal costs per meter
        min_energy_per_m = We * (2.0 * Ku * robot_mass * g + (Ka / v))
        min_time_per_m = Wt * (1.0 / v)
        min_uncert_per_m = Wu * 0.02 # Assumes covered value

        # Absolute minimal possible cost
        c_min = Wd + min_energy_per_m + min_time_per_m + min_uncert_per_m

        return distance * c_min

    def calc_grid_position(self, index: int, min_position: float) -> float:
        """
        calc grid position

        Args:
            index (int): index of a node
            min_position (float): min value of search space

        Returns:
            (float): position of coordinates along the given axis
        """
        return index * self.resolution + min_position

    def calc_xy_index(self, position: float, min_pos: float) -> int:
        """
        calc xy index of node

        Args:
            position (float): position of a node
            min_pos (float): min value of search space

        Returns:
            (int): index of position along the given axis
        """
        return round((position - min_pos) / self.resolution)

    def calc_grid_index(self, node: Node) -> int:
        """
        calc grid index of node

        Args:
            node (Node): node to calculate the index for

        Returns:
            (float): grid index of the node
        """
        return (node.y - self.min_y) * self.x_width + (node.x - self.min_x)

    def verify_node(self, node: Node) -> bool:
        """
        Check if node is acceptable - within limits of search space and free of collisions

        Args:
            node (Node): node to check

        Returns:
            (bool): True if node is acceptable. False otherwise
        """
        px = self.calc_grid_position(node.x, self.origin_x)
        py = self.calc_grid_position(node.y, self.origin_y)

        if (
                px < self.origin_x
                or py < self.origin_y
                or px >= self.max_x
                or py >= self.max_y
        ):
            return False

        # collision check
        return not self.check_node(px, py)

    def check_node(self, x: float, y: float) -> bool:
        """Check position for a collision.

        Args:
            x: World x coordinate of the cell centre.
            y: World y coordinate of the cell centre.

        Returns:
            ``True`` if a collision is detected.
        """
        node_position = [x, y]
        shape = {
            "name": "rectangle",
            "length": self.resolution,
            "width": self.resolution,
        }
        gf = GeometryFactory.create_geometry(**shape)
        geometry = gf.step(np.c_[node_position])
        return self._map.is_collision(geometry)

    @staticmethod
    def get_motion_model() -> list[list[float]]:
        # dx, dy, cost
        return [
            [1, 0, 1],
            [0, 1, 1],
            [-1, 0, 1],
            [0, -1, 1],
            [-1, -1, math.sqrt(2)],
            [-1, 1, math.sqrt(2)],
            [1, -1, math.sqrt(2)],
            [1, 1, math.sqrt(2)],
        ]