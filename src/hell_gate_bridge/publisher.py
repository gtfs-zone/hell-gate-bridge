import logging

import httpx

from hell_gate_bridge.config import Config
from hell_gate_bridge.gtfs import GtfsResolver

from .models import Train

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
) -> None:
    """POST each resolvable train position to cafe-car's /ingest/position.

    Writes the `vehicle:*` contract cafe-car serves from — speed in m/s, epoch
    timestamp, bearing in degrees. Amtrak already knows its trip_id, so no
    server-side resolution is needed.
    """
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set — cannot publish positions")
        return

    url = f"{config.ingest_url.rstrip('/')}/ingest/position"
    headers = {"Authorization": f"Bearer {config.ingest_token}"}

    for train in trains:
        trip_id = resolver.resolve(train.train_num, train.timestamp)
        if trip_id is None:
            log.warning("no trip_id for train %s — skipping", train.train_num)
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
        except httpx.HTTPError as exc:
            log.error("ingest POST failed for train %s: %s", train.train_num, exc)
