import json

import aiomqtt

from .models import Train

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


async def publish_positions(client: aiomqtt.Client, trains: list[Train]) -> None:
    for train in trains:
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
            f"owntracks/amtrak/{train.train_num}",
            json.dumps(payload),
        )
