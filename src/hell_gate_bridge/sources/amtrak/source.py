"""Amtrak live-tracker source.

Wraps the encrypted getTrainsData client (`client.py`) and the shared
`GtfsResolver`, resolving each train to its GTFS trip instance and turning
Amtrak's own per-stop predictions into neutral `StopTimeUpdate`s.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from hell_gate_bridge.gtfs import GtfsResolver, fetch_gtfs
from hell_gate_bridge.sources.base import Source, StopTimeUpdate, VehicleUpdate

from .client import fetch_trains

if TYPE_CHECKING:
    from datetime import datetime

    import httpx

    from hell_gate_bridge.config import Config

    from .models import StopTime, Train

log = logging.getLogger(__name__)

_HEADING_DEGREES: dict[str, int] = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}

_MPH_TO_MS = 0.44704


def _heading_to_degrees(heading: str) -> int | None:
    return _HEADING_DEGREES.get(heading.upper())


def _origin_scheduled(train: Train) -> datetime | None:
    """Scheduled datetime of the train's origin — its stops are origin-first."""
    for stop in train.stops:
        dt = stop.departure.scheduled or stop.arrival.scheduled
        if dt is not None:
            return dt
    return None


def _best_epoch(st: StopTime) -> int | None:
    """Actual time if the train has already passed the stop, else the estimate."""
    dt = st.actual or st.estimated
    return int(dt.timestamp()) if dt is not None else None


def _delay_seconds(st: StopTime) -> int | None:
    """Lateness against Amtrak's own scheduled time; positive means late.

    Uses the same actual-then-estimated precedence as `_best_epoch`, so the delay
    always describes the timestamp being published alongside it.
    """
    dt = st.actual or st.estimated
    if dt is None or st.scheduled is None:
        return None
    return int((dt - st.scheduled).total_seconds())


def _build_stop_time_updates(
    train: Train, stop_seqs: dict[str, int]
) -> list[StopTimeUpdate]:
    """Amtrak's own per-stop predictions → neutral StopTimeUpdate list.

    Station codes not present in the resolved trip's GTFS stop list are skipped
    (Amtrak occasionally reports stops the static feed doesn't carry).
    """
    updates: list[StopTimeUpdate] = []
    for stop in train.stops:
        seq = stop_seqs.get(stop.station_code)
        if seq is None:
            continue
        arrival = _best_epoch(stop.arrival)
        departure = _best_epoch(stop.departure)
        if arrival is None and departure is None:
            continue  # scheduled-only stop carries no realtime information
        update = StopTimeUpdate(stop_id=stop.station_code, stop_sequence=seq)
        # Publish the absolute prediction *and* the delay against schedule —
        # GTFS-RT allows both in one StopTimeEvent, and consumers need the delay
        # to show lateness without carrying the static schedule themselves.
        if arrival is not None:
            update.arrival_time = arrival
            update.arrival_delay = _delay_seconds(stop.arrival)
        if departure is not None:
            update.departure_time = departure
            update.departure_delay = _delay_seconds(stop.departure)
        updates.append(update)
    return updates


class AmtrakSource(Source):
    name = "amtrak"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._resolver: GtfsResolver | None = None

    async def startup(self, http: httpx.AsyncClient) -> None:
        gtfs_path = await fetch_gtfs(
            self._config.gtfs_url, Path(self._config.gtfs_path), http
        )
        self._resolver = GtfsResolver(gtfs_path)

    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        assert self._resolver is not None, "startup() must run before fetch()"
        resolver = self._resolver
        config = self._config

        trains = await fetch_trains(http)
        if config.route_filter:
            trains = [t for t in trains if t.route in config.route_filter]

        updates: list[VehicleUpdate] = []
        unresolved: list[str] = []
        for train in trains:
            resolved = resolver.resolve(
                train.train_num, train.timestamp, origin=_origin_scheduled(train)
            )
            if resolved is None:
                unresolved.append(train.train_num)
                continue
            trip_id, start_date = resolved
            updates.append(
                VehicleUpdate(
                    vehicle_id=config.vehicle_id,
                    trip_id=trip_id,
                    start_date=start_date,
                    timestamp=int(train.timestamp.timestamp()),
                    lat=train.lat,
                    lon=train.lon,
                    speed_mps=round(train.speed_mph * _MPH_TO_MS, 4),
                    bearing=_heading_to_degrees(train.heading),
                    route_id=resolver.route_for(trip_id),
                    stop_time_updates=_build_stop_time_updates(
                        train, resolver.stop_sequences(trip_id)
                    ),
                )
            )

        if unresolved:
            # One line for the batch — this used to be one WARNING per train,
            # which buried everything else in the log.
            log.warning(
                "no trip_id for %d/%d trains (e.g. %s)",
                len(unresolved),
                len(trains),
                ", ".join(unresolved[:10]),
            )
        return updates
