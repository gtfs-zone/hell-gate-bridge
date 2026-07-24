import logging

import httpx

from hell_gate_bridge.config import Config
from hell_gate_bridge.gtfs import GtfsResolver

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


def heading_to_degrees(heading: str) -> int | None:
    return _HEADING_DEGREES.get(heading.upper())


async def publish_positions(
    config: Config, http: httpx.AsyncClient, trains: list[Train], resolver: GtfsResolver
) -> int:
    """POST each resolvable train position to cafe-car's /ingest/position.

    Writes the `vehicle:*` contract cafe-car serves from — speed in m/s, epoch
    timestamp, bearing in degrees. Amtrak already knows its trip_id, so no
    server-side resolution is needed. Returns the number published.
    """
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set — cannot publish positions")
        return 0

    url = f"{config.ingest_url.rstrip('/')}/ingest/position"
    headers = {"Authorization": f"Bearer {config.ingest_token}"}

    published = 0
    unresolved: list[str] = []
    for train in trains:
        trip_id = resolver.resolve(train.train_num, train.timestamp)
        if trip_id is None:
            unresolved.append(train.train_num)
            continue
        body: dict[str, object] = {
            "vehicle_id": config.vehicle_id,
            "trip_id": trip_id,
            "lat": train.lat,
            "lon": train.lon,
            "speed": round(train.speed_mph * _MPH_TO_MS, 4),
            "timestamp": int(train.timestamp.timestamp()),
        }
        bearing = heading_to_degrees(train.heading)
        if bearing is not None:
            body["bearing"] = bearing

        try:
            resp = await http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            published += 1
        except httpx.HTTPError as exc:
            log.error("ingest POST failed for train %s: %r", train.train_num, exc)

    if unresolved:
        # One line for the batch — this used to be one WARNING per train, which
        # buried everything else in the log.
        log.warning(
            "no trip_id for %d/%d trains (e.g. %s)",
            len(unresolved),
            len(trains),
            ", ".join(unresolved[:10]),
        )
    return published


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


def _build_stop_time_updates(train: Train, stop_seqs: dict[str, int]) -> list[dict]:
    """Amtrak's own per-stop predictions → GTFS-RT stop_time_update dicts.

    Station codes not present in the resolved trip's GTFS stop list are skipped
    (Amtrak occasionally reports stops the static feed doesn't carry).
    """
    updates: list[dict] = []
    for stop in train.stops:
        seq = stop_seqs.get(stop.station_code)
        if seq is None:
            continue
        arrival = _best_epoch(stop.arrival)
        departure = _best_epoch(stop.departure)
        if arrival is None and departure is None:
            continue  # scheduled-only stop carries no realtime information
        update: dict[str, object] = {
            "stop_id": stop.station_code,
            "stop_sequence": seq,
        }
        # Publish the absolute prediction *and* the delay against schedule —
        # GTFS-RT allows both in one StopTimeEvent, and consumers need the delay
        # to show lateness without carrying the static schedule themselves.
        if arrival is not None:
            update["arrival_time"] = arrival
            arrival_delay = _delay_seconds(stop.arrival)
            if arrival_delay is not None:
                update["arrival_delay"] = arrival_delay
        if departure is not None:
            update["departure_time"] = departure
            departure_delay = _delay_seconds(stop.departure)
            if departure_delay is not None:
                update["departure_delay"] = departure_delay
        updates.append(update)
    return updates


async def publish_trip_updates(
    config: Config, http: httpx.AsyncClient, trains: list[Train], resolver: GtfsResolver
) -> int:
    """POST each train's Amtrak per-stop predictions to /ingest/trip-update.

    Returns the number published.
    """
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set — cannot publish trip-updates")
        return 0

    url = f"{config.ingest_url.rstrip('/')}/ingest/trip-update"
    headers = {"Authorization": f"Bearer {config.ingest_token}"}

    published = 0
    for train in trains:
        trip_id = resolver.resolve(train.train_num, train.timestamp)
        if trip_id is None:
            continue  # already logged by publish_positions
        updates = _build_stop_time_updates(train, resolver.stop_sequences(trip_id))
        if not updates:
            continue
        body = {
            "trip_id": trip_id,
            "vehicle_id": config.vehicle_id,
            "timestamp": int(train.timestamp.timestamp()),
            "stop_time_updates": updates,
        }
        try:
            resp = await http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            published += 1
        except httpx.HTTPError as exc:
            log.error(
                "trip-update POST failed for train %s: %r", train.train_num, exc
            )
    return published
