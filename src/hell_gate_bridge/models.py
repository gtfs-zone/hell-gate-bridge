from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class StopTime:
    scheduled: datetime | None = None
    estimated: datetime | None = None
    actual: datetime | None = None


@dataclass
class TrainStop:
    station_code: str
    bus: bool
    timezone: str
    status: str
    arrival: StopTime
    departure: StopTime


@dataclass
class Train:
    train_num: str
    route: str
    heading: str
    lat: float
    lon: float
    speed_mph: float
    amtrak_id: str
    timestamp: datetime
    stops: list[TrainStop] = field(default_factory=list)
