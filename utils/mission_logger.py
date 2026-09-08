import matplotlib.pyplot as plt
import numpy as np

from core_msgs.instance_aggregate.mission import POSTURE_WEIGHTS


class MissionLogger:
    def __init__(self  ):
        self._per_mission_log: dict = {}
        self._per_mission_assignement_log = {}



    def update_per_mission_log(self, mission_id, ugv_id, path, cost, assigned_cost):
        ugv_mission_spec = {
            "id": ugv_id,
            "path": path,
            "cost": cost,
            "cost_assigned": assigned_cost
        }

        print(f"[MissionLogger] Logged Mission {mission_id} for UGV {ugv_id} | Cost: {cost:.2f}")

        # Groups all UGV evaluations under the specific mission_id
        self._per_mission_log.setdefault(mission_id, []).append(ugv_mission_spec)


    def update_assignment_log(self, mission_id , ugv_id , sim_time , cost , reason  ):

        assignment_log = {
                    "sim_time":  sim_time,
                    "ugv_id":    ugv_id,
                    "cost":      cost,
                    "reason":    reason,
                }


        self._per_mission_assignement_log.setdefault(mission_id, []).append(assignment_log)




    def draw_mission_costs(self , adt  ):
        """Draws one figure per mission, containing subplots for each UGV evaluated."""

        grid_map = adt.grid_map

        # Calculate spatial extents so the cost map aligns with real-world X/Y path coordinates
        ox, oy = getattr(grid_map, 'ox', 0), getattr(grid_map, 'oy', 0)
        W, H = getattr(grid_map, 'width', 40), getattr(grid_map, 'height', 40)
        extent = [ox, ox + W, oy, oy + H]

        for mission_id, ugv_logs in self._per_mission_log.items():

            # Safely fetch the mission object
            mission = next((m for m in adt.missions if m.mission_id == mission_id), None)
            if not mission:
                print(f"[MissionLogger] Warning: Mission {mission_id} not found.")
                continue

            #  fetch weights
            weights = POSTURE_WEIGHTS[mission.mission_posture]

            # Create a dynamic grid of subplots (1 row, N columns for N robots)
            n_ugvs = len(ugv_logs)
            fig, axes = plt.subplots(1, n_ugvs, figsize=(5 * n_ugvs, 5), squeeze=False)
            fig.suptitle(f"Cost Maps & Paths — Mission: {mission_id}", fontsize=14, fontweight='bold')

            axs = axes.flatten()

            for i, ugv_plan in enumerate(ugv_logs):
                ax = axs[i]
                uid = ugv_plan["id"]

                # Safely find the corresponding UGV object in the OverArchingTwin
                ugv = next((u for u in adt.active_ugvs if getattr(u, 'id', str(id(u))) == uid), None)

                if not ugv:
                    ax.set_title(f"UGV {uid} Not Found")
                    continue

                # Extract parameters, defaulting if they don't exist
                robot_mass      = getattr(ugv, 'mass', 1.0)
                robot_avg_speed = getattr(ugv, 'avg_speed', 1.0)
                robot_anc_drain = getattr(ugv, 'ancillary_drain', 0.0)

                # Generate the 2D Cost Array
                cost_map = grid_map.get_cost_image(
                    weights=weights,
                    robot_mass=robot_mass,
                    v_avg=robot_avg_speed,
                    Ka=robot_anc_drain
                )

                # Plot Cost Map (Note: .T transposes it to match imshow's coordinate system)
                im = ax.imshow(cost_map.T, extent=extent, origin='lower', cmap='YlOrRd', vmin=0, vmax=1)

                # Plot Planned Path
                path = ugv_plan["path"]
                if path is not None and isinstance(path, np.ndarray) and path.ndim == 2:
                    ax.plot(path[0], path[1], 'b-', linewidth=2, label="Planned Path")
                    ax.plot(path[0][-1], path[1][-1], 'go', markersize=6, label="Start")
                    ax.plot(path[0][0], path[1][0], 'rx', markersize=8, label="Goal")

                # Formatting the subplot
                assigned_str = "Error"

                if n_ugvs < 2:
                    assigned_str = ""
                ##Take the latest assingment (Needs to be updated for dynamic replan)
                elif(self._per_mission_assignement_log[mission_id][-1]["ugv_id"] == uid):
                    assigned_str = "(Winner)"
                else:
                    assigned_str = "(Loser)"
                cost = ugv_plan["cost_assigned"]
                if (cost > 1e8):
                   assigned_str = "(Insufficient Battery)"


                ax.set_title(f"UGV: {uid}{assigned_str}\nCost: {ugv_plan['cost']:.2f} , Path Lenght: {len(path[0])} ")
                ax.set_xlabel("x [m]")
                ax.set_ylabel("y [m]")
                ax.legend(loc="upper right", fontsize=8)

            # Add a single colorbar for the whole figure
            fig.subplots_adjust(right=0.9)
            cbar_ax = fig.add_axes([0.92, 0.15, 0.02, 0.7])
            fig.colorbar(im, cax=cbar_ax, label="Normalized Cost")

        # Show all generated figures and block until the user closes them
        plt.show()
