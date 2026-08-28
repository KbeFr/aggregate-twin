from dataclasses import dataclass, field
import time
from typing import Optional

from core_msgs.agents_contract import AgentKind, State2D, Velocity2D
from core_msgs.instance_aggregate.payloads import TwinStatePayload




@dataclass
class AgentEntry:
    """A clean, high-level representation of any robot in the fleet."""

    # -- From discovery
    name: str
    instance_name: str

    kind: AgentKind

    radius : float

    # -- From TwinState
    state: State2D = field(default_factory=State2D)
    velocity: Velocity2D = field(default_factory=Velocity2D)

    battery_pct: Optional[float] = None
    arrive_flag: bool = False
    mission_id: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.last_seen

    def ingest_twin_state(self, payload: TwinStatePayload) -> None:
        """
        Dumb, fast, and safe ingestion.
        No kinematic logic. No array-length guessing.
        """
        # The Aggregate Twin trusts that the Instance Twin sent standardized data
        self.state.x, self.state.y, self.state.theta = payload.state
        self.velocity.linear, self.velocity.angular = payload.velocity

        self.battery_pct = payload.battery_pct
        self.arrive_flag = payload.arrive_flag
        self.mission_id = payload.active_mission_id
        self.last_seen = payload.timestamp or time.time()

    @property
    def xy(self) -> tuple[float, float]:
        return self.state.x, self.state.y