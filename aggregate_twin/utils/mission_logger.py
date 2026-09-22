
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

# TODO rethink the reporting without matplot