"""Source abstraction shared by every upstream tracker.

A `Source` owns everything provider-specific: fetching the upstream feed and
resolving each observation to a GTFS `(trip_id, start_date)` plus per-stop
predictions. It yields a neutral `VehicleUpdate`, so the publisher and poll loop
stay provider-agnostic. cafe-car's ingest requires an explicit `trip_id`, so a
source only emits vehicles it could resolve; unresolved ones are the source's
own concern (it has the context to log them).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


@dataclass
class StopTimeUpdate:
    """A neutral GTFS-RT stop_time_update (absolute times + delay vs schedule)."""

    stop_id: str | None = None
    stop_sequence: int | None = None
    arrival_time: int | None = None  # epoch seconds
    arrival_delay: int | None = None  # seconds, positive = late
    departure_time: int | None = None
    departure_delay: int | None = None


@dataclass
class VehicleUpdate:
    """One resolved vehicle: position + trip instance + per-stop predictions.

    `trip_id` is always set: a source resolves the trip itself rather than
    trusting the device, so cafe-car never has to. `start_date` disambiguates
    concurrent instances of the same trip_id.

    `tracker_id` is a cafe-car `Tracker.id`, the surrogate that selects the feed
    namespace, and it is shared by every vehicle a source publishes. It is not a
    credential: `/ingest/*` is authenticated by the shared `INGEST_API_TOKEN`,
    and the tracker's own secret (`device_key`) is Traccar's business, never
    this repo's.

    `vehicle_id` is the public per-vehicle identity, the GTFS VehicleDescriptor
    id, and it is required: cafe-car keys its live records on
    `vehicle:{tracker_id}:{vehicle_id}`, so a source that omits it collapses its
    whole fleet onto one record, each fix overwriting the last. It must be unique
    within the tracker and stable for as long as the vehicle is out. What counts
    as "the vehicle" is the source's call: buswhere has a device id, Amtrak
    publishes no equipment and identifies a run instead. `vehicle_label` is
    display-only and may be None.

    `current_stop_*`/`current_status` say where the vehicle is *along its trip*,
    which is what lets a consumer place it against the schedule rather than only
    on a map. Every GTFS-RT VehicleStopStatus names a stop, so the three are set
    together or left None together, and cafe-car rejects a status without a stop.
    A source that cannot tell where the vehicle is leaves all three None, and
    the feed then reports nothing rather than guessing.
    """

    tracker_id: str
    vehicle_id: str  # public VehicleDescriptor.id, and half the cafe-car key
    trip_id: str
    timestamp: int  # epoch seconds
    lat: float
    lon: float
    start_date: str | None = None  # YYYYMMDD
    vehicle_label: str | None = None  # public VehicleDescriptor.label
    route_id: str | None = None
    speed_mps: float | None = None
    bearing: float | None = None  # degrees
    current_stop_sequence: int | None = None
    current_stop_id: str | None = None
    # GTFS-RT VehicleStopStatus by name: INCOMING_AT | STOPPED_AT | IN_TRANSIT_TO
    current_status: str | None = None
    stop_time_updates: list[StopTimeUpdate] = field(default_factory=list)


class Source(ABC):
    """A provider-specific poller that yields resolved `VehicleUpdate`s."""

    name: str

    async def startup(self, http: httpx.AsyncClient) -> None:  # noqa: B027
        """One-time async setup (e.g. download + index static GTFS).

        Optional hook: sources with no async setup can leave it as the default
        no-op, so it is deliberately concrete rather than abstract.
        """

    @abstractmethod
    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        """Poll the upstream feed and return resolved vehicles."""
