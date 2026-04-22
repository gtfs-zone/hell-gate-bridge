import json
import logging

import aiomqtt

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

_MPH_TO_KPH = 1.60934


def heading_to_degrees(heading: str) -> int | None:
    return _HEADING_DEGREES.get(heading.upper())


async def publish_positions(
    config: Config, client: aiomqtt.Client, trains: list[Train], resolver: GtfsResolver
) -> None:
    for train in trains:
        trip_id = resolver.resolve(train.train_num, train.timestamp)
        if trip_id is None:
            log.warning("no trip_id for train %s — skipping", train.train_num)
            continue
        payload = {
            "_type": "location",
            "lat": train.lat,
            "lon": train.lon,
            "tst": int(train.timestamp.timestamp()),
        }
        cog = heading_to_degrees(train.heading)
        if cog is not None:
            payload["cog"] = cog
        payload["vel"] = round(train.speed_mph * _MPH_TO_KPH)

        await client.publish(
            f"owntracks/{config.mqtt_username}/{trip_id}",
            json.dumps(payload),
        )
