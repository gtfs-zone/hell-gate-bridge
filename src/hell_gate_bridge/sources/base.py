"""Source abstraction shared by every upstream tracker.

A `Source` owns everything provider-specific: fetching the upstream feed and
resolving each observation to a GTFS `(trip_id, start_date)` plus per-stop
predictions. It yields a neutral `VehicleUpdate`, so the publisher and poll loop
stay provider-agnostic. cafe-car's ingest requires an explicit `trip_id`, so a
source only emits vehicles it could resolve — unresolved ones are the source's
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

    `trip_id` is always set — a source resolves the trip itself rather than
    trusting the device, so cafe-car never has to. `start_date` disambiguates
    concurrent instances of the same trip_id.
    """

    vehicle_id: str
    trip_id: str
    timestamp: int  # epoch seconds
    lat: float
    lon: float
    start_date: str | None = None  # YYYYMMDD
    route_id: str | None = None
    speed_mps: float | None = None
    bearing: float | None = None  # degrees
    stop_time_updates: list[StopTimeUpdate] = field(default_factory=list)


class Source(ABC):
    """A provider-specific poller that yields resolved `VehicleUpdate`s."""

    name: str

    async def startup(self, http: httpx.AsyncClient) -> None:  # noqa: B027
        """One-time async setup (e.g. download + index static GTFS).

        Optional hook — sources with no async setup can leave it as the default
        no-op, so it is deliberately concrete rather than abstract.
        """

    @abstractmethod
    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        """Poll the upstream feed and return resolved vehicles."""
