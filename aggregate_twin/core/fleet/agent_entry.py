import time
import logging

from dataclasses import dataclass, field
from typing import Optional

from aggregate_twin.core.fleet.sensor_footprint import SensorFootprint

from core_msgs.agents_contract import AgentKind, State2D, Velocity2D
from core_msgs.instance_aggregate.payloads import TwinStatePayload

logger = logging.getLogger(__name__)


@dataclass
class AgentEntry:
    """A clean, high-level representation of any robot in the fleet."""

    # -- From discovery
    name: str
    id : int
    instance_name: str
    kind: AgentKind
    type : Optional[str]
    radius : float

    # -- From TwinState
    state: State2D = field(default_factory=State2D)
    velocity: Velocity2D = field(default_factory=Velocity2D)

    battery_pct: Optional[float] = None
    arrive_flag: bool = False
    mission_id: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    # -- From obstacle reports: how far its sensors reach, in its own body frame
    sensor_footprint: SensorFootprint = field(default_factory=SensorFootprint)

    def ingest_twin_state(self, payload: TwinStatePayload) -> None:

        s = tuple(payload.state)
        if len(s) < 2:
            logger.warning("agent=%s sent state of length %d, ignoring", self.name, len(s))
            return
        self.state.x, self.state.y = s[0], s[1]
        self.state.theta = s[2] if len(s) > 2 else self.state.theta

        self.battery_pct = payload.battery_pct
        self.arrive_flag = payload.arrive_flag
        self.mission_id = payload.active_mission_id
        self.last_seen = time.time()

    def observe_detection(self, x: float, y: float) -> bool:
        """The agent reported something at world (x, y): its sensors reach that far."""
        return self.sensor_footprint.observe((self.state.x, self.state.y, self.state.theta), (x, y))

    @property
    def age(self) -> float:
        return time.time() - self.last_seen

    @property
    def xy(self) -> tuple[float, float]:
        return self.state.x, self.state.y